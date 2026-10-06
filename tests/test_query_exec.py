"""End-to-end tests for `graph-index query-exec`."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from graph_index.cli import main

SCHEMA = """\
type Query {
  users(id: ID, name: String, status: String): [User!]!
  user(id: ID!): User
  thing(owner: ID): Thing
  teams: [Team!]!
}

enum Role {
  ADMIN
  MEMBER
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: String
  role: Role
  tags: [String!]
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  group: Org! @link(local: "team_id", target: "id")
  reviews: [Review!]! @link(local: "id", target: "user_id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
  league_id: ID
  league: League @link(local: "league_id", target: "id")
}

type League @entity(name: "leagues", key: "id") {
  id: ID!
  title: String!
}

type Org @entity(name: "orgs", key: "id") {
  id: ID!
  name: String!
}

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  score: Int
}

type Thing @entity(name: "things", key: "id") {
  id: ID!
  owner: ID
}
"""

EVENTS = "\n".join(
    [
        json.dumps(row)
        for row in [
            {"op": "INSERT", "entity": "leagues", "before": None,
             "after": {"id": 5, "title": "L1"}},
            {"op": "INSERT", "entity": "orgs", "before": None,
             "after": {"id": 9, "name": "the-org"}},
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core", "league_id": 5}},
            {"op": "INSERT", "entity": "reviews", "before": None,
             "after": {"id": "r1", "user_id": 1, "score": 10}},
            {"op": "INSERT", "entity": "reviews", "before": None,
             "after": {"id": "r2", "user_id": 1, "score": 20}},
            {"op": "INSERT", "entity": "users", "before": None,
             "after": {"id": 1, "name": "ada", "status": "active",
                       "role": "ADMIN", "tags": ["a", "b"], "team_id": 9}},
            {"op": "INSERT", "entity": "users", "before": None,
             "after": {"id": 2, "name": "bob", "status": "idle",
                       "role": "MEMBER", "tags": [], "team_id": None}},
            {"op": "UPDATE", "entity": "users",
             "before": {"id": 1, "name": "ada"},
             "after": {"id": 1, "name": "ada2", "status": "active",
                       "role": "ADMIN", "tags": ["x"], "team_id": 9}},
            {"op": "DELETE", "entity": "reviews",
             "before": {"id": "r2", "user_id": 1, "score": 20},
             "after": None},
        ]
    ]
) + "\n"

COMPOSITE_SCHEMA = """\
type Query {
  transfers(chain_id: Int, id: ID): [Transfer!]!
}

type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
  chain_id: Int!
  id: ID!
  amount: Float
  token_chain: Int
  token_id: ID
  token: Token @link(local: ["token_chain", "token_id"], target: ["chain_id", "id"])
  lines: [Line!]! @link(local: ["chain_id", "id"], target: ["chain", "ref"])
}

type Token @entity(name: "tokens", key: ["chain_id", "id"]) {
  chain_id: Int!
  id: ID!
  symbol: String!
}

type Line @entity(name: "lines", key: "id") {
  id: ID!
  chain: Int
  ref: ID
  qty: Int
}
"""


def _event(op, entity, before, after):
    return json.dumps({"op": op, "entity": entity, "before": before,
                       "after": after})


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content, mode="w"):
        path = os.path.join(self.tmp.name, name)
        with open(path, mode) as handle:
            handle.write(content)
        return path

    def run_cli(self, query, variables="{}", schema=SCHEMA, events=EVENTS,
                operation=None):
        schema_path = self._write("schema.graphql", schema)
        query_path = self._write("query.graphql", query)
        variables_path = self._write("variables.json", variables)
        events_path = self._write("events.ndjson", events)
        argv = ["query-exec", "--schema", schema_path, "--query", query_path,
                "--variables", variables_path, "--events", events_path]
        if operation is not None:
            argv += ["--operation", operation]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_error(self, code, stdout, stderr, expected_code):
        self.assertEqual(code, 2, stdout)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], expected_code)
        self.assertIsInstance(payload["message"], str)
        self.assertTrue(payload["message"])

    def assert_data(self, code, stdout, stderr):
        self.assertEqual(stderr, "")
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertIn("data", payload)
        return payload["data"]

    # -- happy paths ---------------------------------------------------------

    def test_list_root_returns_all_in_insert_order(self):
        data = self.assert_data(*self.run_cli("{ users { id name } }"))
        self.assertEqual(data["users"], [
            {"id": 1, "name": "ada2"},
            {"id": 2, "name": "bob"},
        ])

    def test_root_filter_equality_and_variables(self):
        query = "query ($name: String) { users(name: $name) { id } }"
        data = self.assert_data(*self.run_cli(query, '{"name": "bob"}'))
        self.assertEqual(data["users"], [{"id": 2}])

    def test_filter_literal_no_match_is_empty_list(self):
        data = self.assert_data(*self.run_cli('{ users(status: "nope") { id } }'))
        self.assertEqual(data["users"], [])

    def test_single_root_found_null_and_aliases(self):
        query = """
        {
          me: user(id: 1) { userId: id name }
          missing: user(id: 99) { id }
        }
        """
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data["me"], {"userId": 1, "name": "ada2"})
        self.assertIsNone(data["missing"])

    def test_scalars_enums_and_list_scalars_passthrough(self):
        data = self.assert_data(*self.run_cli("{ users { id role tags status } }"))
        self.assertEqual(data["users"][0],
                         {"id": 1, "role": "ADMIN", "tags": ["x"],
                          "status": "active"})
        self.assertEqual(data["users"][1],
                         {"id": 2, "role": "MEMBER", "tags": [],
                          "status": "idle"})

    def test_object_link_resolved_and_null_when_local_null(self):
        data = self.assert_data(
            *self.run_cli("{ users { id team { id name } } }")
        )
        self.assertEqual(data["users"][0]["team"], {"id": 9, "name": "core"})
        self.assertIsNone(data["users"][1]["team"])

    def test_multi_level_object_link(self):
        data = self.assert_data(
            *self.run_cli("{ users { id team { name league { title } } } }")
        )
        self.assertEqual(
            data["users"][0]["team"],
            {"name": "core", "league": {"title": "L1"}},
        )
        self.assertIsNone(data["users"][1]["team"])

    def test_list_link_matches_in_target_insert_order(self):
        # r2 was deleted, so only r1 remains for user 1.
        data = self.assert_data(
            *self.run_cli("{ users { id reviews { id score } } }")
        )
        self.assertEqual(data["users"][0]["reviews"], [
            {"id": "r1", "score": 10},
        ])
        self.assertEqual(data["users"][1]["reviews"], [])

    def test_list_link_non_primary_key_target(self):
        # reviews.user_id is not the reviews primary key; equality matching
        # still collects every current target in target insert order.
        data = self.assert_data(
            *self.run_cli("{ users { id reviews { id } } }")
        )
        self.assertEqual(data["users"][0]["reviews"], [{"id": "r1"}])
        self.assertEqual(data["users"][1]["reviews"], [])

    def test_fragments_expanded(self):
        query = """
        { users { id ...Bits team { ...TeamBits } } }
        fragment Bits on User { name }
        fragment TeamBits on Team { id name }
        """
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data["users"][0],
                         {"id": 1, "name": "ada2",
                          "team": {"id": 9, "name": "core"}})

    def test_variable_default_used_when_not_provided(self):
        query = 'query ($name: String = "bob") { users(name: $name) { id } }'
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data["users"], [{"id": 2}])

    def test_operation_selected_by_name(self):
        query = """
        query A { users { id } }
        query B { teams { name } }
        """
        data = self.assert_data(*self.run_cli(query, operation="B"))
        self.assertEqual(data, {"teams": [{"name": "core"}]})

    def test_multiple_root_fields_in_one_response(self):
        query = "{ users { id } teams { id } }"
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(set(data.keys()), {"users", "teams"})
        self.assertEqual(data["teams"], [{"id": 9}])

    def test_blank_event_lines_skipped(self):
        events = "\n" + EVENTS.replace("\n", "\n\n")
        data = self.assert_data(*self.run_cli("{ users { id } }", events=events))
        self.assertEqual(len(data["users"]), 2)

    def test_events_for_unreachable_entities_ignored(self):
        events = (
            _event("INSERT", "widgets", None, {"id": 1, "nope": True}) + "\n"
            + _event("DELETE", "widgets", {"id": 1}, None) + "\n"
            + _event("INSERT", "teams", None, {"id": 1, "name": "solo"}) + "\n"
        )
        data = self.assert_data(*self.run_cli("{ teams { name } }", events=events))
        self.assertEqual(data["teams"], [{"name": "solo"}])

    # -- ordering semantics ---------------------------------------------------

    def test_update_replaces_in_place(self):
        schema = (
            "type Query { things: [Thing!]! }\n"
            'type Thing @entity(name: "things", key: "id") { id: ID! owner: ID }\n'
        )
        events = "\n".join([
            _event("INSERT", "things", None, {"id": "a", "owner": 1}),
            _event("INSERT", "things", None, {"id": "b", "owner": 1}),
            _event("UPDATE", "things", {"id": "a"}, {"id": "a", "owner": 2}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ things { id owner } }", schema=schema, events=events))
        self.assertEqual(data["things"], [
            {"id": "a", "owner": 2}, {"id": "b", "owner": 1},
        ])

    def test_delete_then_reinsert_goes_to_end(self):
        schema = (
            "type Query { things: [Thing!]! }\n"
            'type Thing @entity(name: "things", key: "id") { id: ID! v: Int }\n'
        )
        events = "\n".join([
            _event("INSERT", "things", None, {"id": "a", "v": 1}),
            _event("INSERT", "things", None, {"id": "b", "v": 2}),
            _event("DELETE", "things", {"id": "a"}, None),
            _event("INSERT", "things", None, {"id": "a", "v": 10}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ things { id v } }", schema=schema, events=events))
        self.assertEqual(data["things"], [
            {"id": "b", "v": 2}, {"id": "a", "v": 10},
        ])

    def test_list_link_order_follows_target_reinsertion(self):
        events = "\n".join([
            _event("INSERT", "reviews", None, {"id": "r1", "user_id": 1, "score": 1}),
            _event("INSERT", "reviews", None, {"id": "r2", "user_id": 1, "score": 2}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "team_id": None}),
            _event("DELETE", "reviews", {"id": "r1", "user_id": 1}, None),
            _event("INSERT", "reviews", None, {"id": "r1", "user_id": 1, "score": 11}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id reviews { id score } } }", events=events))
        self.assertEqual(data["users"][0]["reviews"], [
            {"id": "r2", "score": 2}, {"id": "r1", "score": 11},
        ])

    # -- composite keys -------------------------------------------------------

    def test_composite_object_link(self):
        events = "\n".join([
            _event("INSERT", "tokens", None,
                   {"chain_id": 1, "id": "t1", "symbol": "NEW"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 1, "id": "x1",
                    "token_chain": 1, "token_id": "t1"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 2, "id": "x2",
                    "token_chain": None, "token_id": None}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ transfers { chain_id id token { symbol } } }",
            schema=COMPOSITE_SCHEMA, events=events))
        self.assertEqual(data["transfers"], [
            {"chain_id": 1, "id": "x1", "token": {"symbol": "NEW"}},
            {"chain_id": 2, "id": "x2", "token": None},
        ])

    def test_composite_list_link_on_non_key_targets(self):
        events = "\n".join([
            _event("INSERT", "lines", None,
                   {"id": "a", "chain": 1, "ref": "R", "qty": 2}),
            _event("INSERT", "lines", None,
                   {"id": "b", "chain": 1, "ref": "R", "qty": 5}),
            _event("INSERT", "lines", None,
                   {"id": "c", "chain": 2, "ref": "R", "qty": 9}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 1, "id": "R"}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ transfers { id lines { id qty } } }",
            schema=COMPOSITE_SCHEMA, events=events))
        self.assertEqual(data["transfers"][0]["lines"], [
            {"id": "a", "qty": 2}, {"id": "b", "qty": 5},
        ])

    def test_composite_filter(self):
        events = "\n".join([
            _event("INSERT", "transfers", None, {"chain_id": 1, "id": "a"}),
            _event("INSERT", "transfers", None, {"chain_id": 2, "id": "a"}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ transfers(chain_id: 2, id: \"a\") { chain_id id } }",
            schema=COMPOSITE_SCHEMA, events=events))
        self.assertEqual(data["transfers"], [{"chain_id": 2, "id": "a"}])

    # -- unsupported operations ----------------------------------------------

    def test_mutation_unsupported(self):
        self.assert_error(
            *self.run_cli("mutation { user(id: 1) { id } }"),
            "UnsupportedOperation",
        )

    def test_subscription_unsupported(self):
        self.assert_error(
            *self.run_cli("subscription { users { id } }"),
            "UnsupportedOperation",
        )

    def test_mutation_selected_by_name_unsupported(self):
        query = "query A { users { id } } mutation B { user(id: 1) { id } }"
        self.assert_error(
            *self.run_cli(query, operation="B"), "UnsupportedOperation"
        )

    # -- operation selection ---------------------------------------------------

    def test_multiple_operations_require_name(self):
        query = "query A { users { id } } query B { teams { id } }"
        self.assert_error(*self.run_cli(query), "InvalidRequest")

    def test_unknown_operation_name(self):
        self.assert_error(
            *self.run_cli("query A { users { id } }", operation="Nope"),
            "InvalidOperation",
        )

    def test_no_operations(self):
        self.assert_error(
            *self.run_cli("fragment F on User { id }"), "InvalidRequest"
        )

    # -- single-object root semantics ------------------------------------------

    def test_single_root_multiple_matches_is_query_error(self):
        events = "\n".join([
            _event("INSERT", "things", None, {"id": "a", "owner": 7}),
            _event("INSERT", "things", None, {"id": "b", "owner": 7}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("{ thing(owner: 7) { id } }", events=events),
            "QueryError",
        )

    # -- GraphQL / variables / mapping errors ---------------------------------

    def test_query_parse_error(self):
        self.assert_error(*self.run_cli("{ users { id "), "ParseError")

    def test_unknown_root_field(self):
        self.assert_error(*self.run_cli("{ nope { id } }"), "UnknownField")

    def test_unknown_nested_field(self):
        self.assert_error(*self.run_cli("{ users { nope } }"), "UnknownField")

    def test_unknown_argument(self):
        self.assert_error(
            *self.run_cli("{ users(nope: 1) { id } }"), "UnknownField"
        )

    def test_missing_required_argument(self):
        self.assert_error(*self.run_cli("{ user { id } }"), "UnknownField")

    def test_scalar_field_with_selection(self):
        self.assert_error(
            *self.run_cli("{ users { id { x } } }"), "InvalidQuery"
        )

    def test_object_field_without_selection(self):
        self.assert_error(*self.run_cli("{ users { team } }"), "InvalidQuery")

    def test_empty_selection(self):
        self.assert_error(*self.run_cli("{ users { } }"), "InvalidQuery")

    def test_unknown_fragment(self):
        self.assert_error(*self.run_cli("{ users { ...Nope } }"), "InvalidQuery")

    def test_root_return_type_not_entity(self):
        schema = SCHEMA + "extend type Query { version: Version }\n" \
                          "type Version { tag: String }\n"
        self.assert_error(
            *self.run_cli("{ version { tag } }", schema=schema), "UnknownEntity"
        )

    def test_object_link_without_mapping_is_mapping_error(self):
        schema = SCHEMA.replace(
            'team: Team @link(local: "team_id", target: "id")', "team: Team"
        )
        self.assert_error(
            *self.run_cli("{ users { team { id } } }", schema=schema),
            "MappingError",
        )

    def test_object_link_target_not_primary_key_is_invalid_join(self):
        schema = COMPOSITE_SCHEMA.replace(
            'target: ["chain_id", "id"]', 'target: ["chain_id", "symbol"]'
        )
        self.assert_error(
            *self.run_cli(
                "{ transfers { token { symbol } } }", schema=schema
            ),
            "InvalidJoin",
        )

    def test_list_link_target_non_scalar_is_mapping_error(self):
        schema = SCHEMA + (
            "extend type User { codes: [String!] }\n"
            "extend type User {"
            " coded: [Review!]! @link(local: \"codes\", target: \"score\")}\n"
        )
        self.assert_error(
            *self.run_cli("{ users { coded { id } } }", schema=schema),
            "MappingError",
        )

    def test_variable_missing(self):
        self.assert_error(
            *self.run_cli(
                "query ($id: ID!) { user(id: $id) { id } }"
            ),
            "VariablesError",
        )

    def test_variable_wrong_type(self):
        self.assert_error(
            *self.run_cli(
                "{ users(name: $n) { id } }", '{"n": 7}'
            ),
            "VariablesError",
        )

    def test_variables_file_not_object(self):
        self.assert_error(*self.run_cli("{ users { id } }", "[1]"),
                          "VariablesError")

    # -- event structure errors -----------------------------------------------

    def _events_error(self, events, expected="EventError", query="{ users { id } }"):
        self.assert_error(*self.run_cli(query, events=events), expected)

    def test_event_invalid_json(self):
        self._events_error("{not json\n")

    def test_event_not_an_object(self):
        self._events_error("[1]\n")

    def test_event_missing_key_member(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "after": {}}\n'
        )

    def test_event_unknown_op(self):
        self._events_error(
            '{"op": "UPSERT", "entity": "users", "before": null, "after": {}}\n'
        )

    def test_event_empty_entity(self):
        self._events_error(
            '{"op": "INSERT", "entity": "", "before": null, "after": {"id": 1}}\n'
        )

    def test_event_snapshot_not_object(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": 5}\n'
        )

    def test_insert_without_after(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": null}\n'
        )

    def test_delete_without_before(self):
        self._events_error(
            '{"op": "DELETE", "entity": "users", "before": null, "after": null}\n'
        )

    def test_mapped_event_missing_primary_key_field(self):
        self._events_error(
            _event("INSERT", "users", None, {"name": "ada"}) + "\n"
        )

    def test_mapped_event_null_primary_key(self):
        self._events_error(
            _event("INSERT", "users", None, {"id": None, "name": "ada"}) + "\n"
        )

    def test_mapped_event_composite_primary_key_value(self):
        events = _event(
            "INSERT", "transfers", None, {"chain_id": [1], "id": "x"}
        ) + "\n"
        self.assert_error(
            *self.run_cli(
                "{ transfers { id } }", schema=COMPOSITE_SCHEMA, events=events
            ),
            "EventError",
        )

    def test_delete_before_with_missing_key(self):
        self._events_error(
            '{"op": "DELETE", "entity": "users", "before": {"name": "x"}, '
            '"after": null}\n'
        )

    def test_all_lines_validated_before_any_snapshot_change(self):
        events = (
            _event("INSERT", "teams", None, {"id": 1, "name": "ok"}) + "\n"
            "{broken json\n"
        )
        self.assert_error(
            *self.run_cli("{ teams { name } }", events=events), "EventError"
        )

    # -- projection / shape errors --------------------------------------------

    def test_snapshot_missing_selected_field(self):
        self._events_error(
            _event("INSERT", "users", None, {"id": 1}) + "\n",
            query="{ users { name } }",
        )

    def test_scalar_shape_mismatch_string(self):
        self._events_error(
            _event("INSERT", "users", None,
                   {"id": 1, "name": 5, "team_id": None}) + "\n",
            query="{ users { name } }",
        )

    def test_scalar_shape_mismatch_int(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r", "user_id": 1, "score": "high"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "team_id": None}),
        ]) + "\n"
        self._events_error(
            events,
            query="{ users { reviews { score } } }",
        )

    def test_list_field_value_not_array(self):
        self._events_error(
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "tags": "x", "team_id": None}) + "\n",
            query="{ users { tags } }",
        )

    def test_enum_value_not_string(self):
        self._events_error(
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "role": 3, "team_id": None}) + "\n",
            query="{ users { role } }",
        )

    def test_non_null_scalar_null(self):
        self._events_error(
            _event("INSERT", "users", None,
                   {"id": 1, "name": None, "team_id": None}) + "\n",
            query="{ users { name } }",
        )

    def test_filter_field_shape_mismatch(self):
        schema = (
            "type Query { users(name: String): [User!]! }\n"
            'type User @entity(name: "users", key: "id") '
            "{ id: ID! name: String }\n"
        )
        events = _event("INSERT", "users", None,
                        {"id": 1, "name": 7}) + "\n"
        self.assert_error(
            *self.run_cli('{ users(name: "x") { id } }',
                          schema=schema, events=events),
            "EventError",
        )

    def test_snapshot_missing_filter_field(self):
        events = _event("INSERT", "users", None, {"id": 1}) + "\n"
        self.assert_error(
            *self.run_cli('{ users(status: "active") { id } }', events=events),
            "EventError",
        )

    def test_dangling_object_link_is_event_error(self):
        events = _event("INSERT", "users", None,
                        {"id": 1, "name": "a", "team_id": 77}) + "\n"
        self.assert_error(
            *self.run_cli("{ users { id team { name } } }", events=events),
            "EventError",
        )

    def test_null_local_on_non_null_object_link_is_event_error(self):
        events = _event("INSERT", "users", None,
                        {"id": 1, "name": "a", "team_id": None}) + "\n"
        self.assert_error(
            *self.run_cli("{ users { id group { name } } }", events=events),
            "EventError",
        )

    def test_missing_local_field_is_event_error(self):
        events = _event("INSERT", "users", None,
                        {"id": 1, "name": "a"}) + "\n"
        self.assert_error(
            *self.run_cli("{ users { id team { name } } }", events=events),
            "EventError",
        )

    def test_composite_local_partial_null_is_null_relationship(self):
        events = _event(
            "INSERT", "transfers", None,
            {"chain_id": 1, "id": "x", "token_chain": None, "token_id": "t"},
        ) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ transfers { id token { symbol } } }",
            schema=COMPOSITE_SCHEMA, events=events))
        self.assertIsNone(data["transfers"][0]["token"])

    def test_target_snapshot_missing_selected_field_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "team_id": 9}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("{ users { team { name } } }", events=events),
            "EventError",
        )

    def test_list_target_missing_match_field_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "reviews", None, {"id": "r", "score": 1}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "team_id": None}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("{ users { reviews { score } } }", events=events),
            "EventError",
        )

    def test_deleted_target_makes_object_link_fail(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "team_id": 9}),
            _event("DELETE", "teams", {"id": 9, "name": "core"}, None),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("{ users { team { name } } }", events=events),
            "EventError",
        )

    # -- io errors --------------------------------------------------------------

    def test_missing_events_file(self):
        schema_path = self._write("schema.graphql", SCHEMA)
        query_path = self._write("query.graphql", "{ users { id } }")
        variables_path = self._write("variables.json", "{}")
        argv = ["query-exec", "--schema", schema_path, "--query", query_path,
                "--variables", variables_path,
                "--events", "/nonexistent/events.ndjson"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")


if __name__ == "__main__":
    unittest.main()
