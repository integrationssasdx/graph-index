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
  users(id: ID, status: String, role: Role): [User!]!
  user(id: ID): User
  teams: [Team!]!
}

type Mutation {
  createUser(name: String!): User
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
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
}
"""

EVENTS = "\n".join(
    [
        json.dumps(row)
        for row in [
            {"op": "INSERT", "entity": "users", "before": None,
             "after": {"id": 1, "name": "ada", "status": "active",
                       "role": "ADMIN", "tags": ["a", "b"], "team_id": 9}},
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            {"op": "INSERT", "entity": "users", "before": None,
             "after": {"id": 2, "name": "bob", "status": "banned",
                       "role": "MEMBER", "tags": [], "team_id": None}},
            {"op": "UPDATE", "entity": "users",
             "before": {"id": 2, "name": "bob", "status": "banned",
                        "role": "MEMBER", "tags": [], "team_id": None},
             "after": {"id": 2, "name": "bob", "status": "active",
                       "role": "MEMBER", "tags": ["x"], "team_id": 9}},
        ]
    ]
) + "\n"

NESTED_SCHEMA = """\
type Query {
  users(id: ID, status: String): [User!]!
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: String
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  group: Org! @link(local: "team_id", target: "id")
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
"""

LIST_SCHEMA = """\
type Query {
  users(id: ID, status: String): [User!]!
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: String
  org_id: ID
  org: Org @link(local: "org_id", target: "id")
  mates: [User!]! @link(local: "org_id", target: "org_id")
  reviews: [Review!]! @link(local: "id", target: "user_id")
}

type Org @entity(name: "orgs", key: "id") {
  id: ID!
  name: String!
  members: [User!]! @link(local: "id", target: "org_id")
}

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  score: Int
}
"""

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
}

type Token @entity(name: "tokens", key: ["chain_id", "id"]) {
  chain_id: Int!
  id: ID!
  symbol: String!
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
        argv = ["query-exec", "--schema", schema_path,
                "--query", query_path,
                "--variables", variables_path,
                "--events", events_path]
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
        return json.loads(stdout)["data"]

    # -- happy paths ---------------------------------------------------------

    def test_list_root_returns_all_in_insert_order(self):
        data = self.assert_data(*self.run_cli("{ users { id name } }"))
        self.assertEqual(data, {"users": [
            {"id": 1, "name": "ada"},
            {"id": 2, "name": "bob"},
        ]})

    def test_list_root_filter_with_variable_and_literal(self):
        query = """
        query Q($status: String) {
          users(status: $status, role: ADMIN) { id }
        }
        """
        data = self.assert_data(*self.run_cli(query, '{"status": "active"}'))
        self.assertEqual(data, {"users": [{"id": 1}]})

    def test_variable_default_used_when_not_provided(self):
        query = "query ($status: String = \"banned\") { users(status: $status) { id } }"
        # bob was updated to active, so the banned default matches nothing
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data, {"users": []})

    def test_single_root_returns_unique_match(self):
        data = self.assert_data(*self.run_cli('{ user(id: 2) { id name } }'))
        self.assertEqual(data, {"user": {"id": 2, "name": "bob"}})

    def test_single_root_no_match_is_null(self):
        data = self.assert_data(*self.run_cli('{ user(id: 77) { id } }'))
        self.assertEqual(data, {"user": None})

    def test_single_root_multiple_matches_is_query_error(self):
        self.assert_error(
            *self.run_cli("{ user { id } }"), "QueryError"
        )

    def test_alias_and_list_preserved(self):
        data = self.assert_data(*self.run_cli("{ me: users { userId: id tags } }"))
        self.assertEqual(data, {"me": [
            {"userId": 1, "tags": ["a", "b"]},
            {"userId": 2, "tags": ["x"]},
        ]})

    def test_multiple_roots(self):
        data = self.assert_data(
            *self.run_cli("{ users { id } teams { id name } }")
        )
        self.assertEqual(data, {
            "users": [{"id": 1}, {"id": 2}],
            "teams": [{"id": 9, "name": "core"}],
        })

    def test_fragments_expanded(self):
        query = """
        { users { id ...Bits } }
        fragment Bits on User { name }
        """
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data, {"users": [
            {"id": 1, "name": "ada"},
            {"id": 2, "name": "bob"},
        ]})

    def test_operation_selected_by_name(self):
        query = """
        mutation Ignore { createUser(name: "x") { id } }
        query A { users { id } }
        query B { teams { id } }
        """
        data = self.assert_data(*self.run_cli(query, operation="B"))
        self.assertEqual(data, {"teams": [{"id": 9}]})

    def test_empty_events_gives_empty_and_null_roots(self):
        data = self.assert_data(
            *self.run_cli("{ users { id } user(id: 1) { id } }", events="")
        )
        self.assertEqual(data, {"users": [], "user": None})

    def test_update_replaces_in_place_and_delete_removes(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "status": "active", "team_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 3, "name": "cy", "status": "active", "team_id": 9}),
            _event("DELETE", "users",
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9},
                   None),
            _event("UPDATE", "users",
                   {"id": 3, "name": "cy", "status": "active", "team_id": 9},
                   {"id": 3, "name": "cy2", "status": "active", "team_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada2", "status": "active", "team_id": 9}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli("{ users { id name } }",
                                              events=events))
        # id 2 and 3 keep their positions; re-inserted id 1 lands at the end
        self.assertEqual(data, {"users": [
            {"id": 2, "name": "bob"},
            {"id": 3, "name": "cy2"},
            {"id": 1, "name": "ada2"},
        ]})

    def test_blank_event_lines_skipped(self):
        events = "\n" + EVENTS.replace("\n", "\n\n")
        data = self.assert_data(*self.run_cli("{ users { id } }",
                                              events=events))
        self.assertEqual(data, {"users": [{"id": 1}, {"id": 2}]})

    # -- operation selection errors -------------------------------------------

    def test_mutation_rejected(self):
        self.assert_error(
            *self.run_cli("mutation { createUser(name: \"x\") { id } }"),
            "UnsupportedOperation",
        )

    def test_subscription_rejected(self):
        self.assert_error(
            *self.run_cli("subscription { users { id } }"),
            "UnsupportedOperation",
        )

    def test_mutation_selected_by_name_rejected(self):
        query = "query A { users { id } } mutation B { createUser(name: \"x\") { id } }"
        self.assert_error(*self.run_cli(query, operation="B"),
                          "UnsupportedOperation")

    def test_multiple_operations_require_name(self):
        self.assert_error(
            *self.run_cli("query A { users { id } } query B { users { id } }"),
            "InvalidRequest",
        )

    def test_invalid_operation_name(self):
        self.assert_error(
            *self.run_cli("query A { users { id } }", operation="Nope"),
            "InvalidOperation",
        )

    def test_no_operations(self):
        self.assert_error(
            *self.run_cli("fragment F on User { id }"), "InvalidRequest"
        )

    # -- selection / filter errors ----------------------------------------------

    def test_unknown_root_field(self):
        self.assert_error(*self.run_cli("{ nope { id } }"), "UnknownField")

    def test_unknown_nested_field(self):
        self.assert_error(*self.run_cli("{ users { nope } }"), "InvalidQuery")

    def test_unknown_argument(self):
        self.assert_error(*self.run_cli("{ users(nope: 1) { id } }"),
                          "UnknownField")

    def test_missing_required_argument(self):
        schema = SCHEMA.replace("user(id: ID): User", "user(id: ID!): User")
        self.assert_error(*self.run_cli("{ user { id } }", schema=schema),
                          "UnknownField")

    def test_root_return_type_not_entity(self):
        schema = SCHEMA + "extend type Query { version: Version }\n" \
                          "type Version { tag: String }\n"
        self.assert_error(*self.run_cli("{ version { tag } }", schema=schema),
                          "UnknownEntity")

    def test_no_query_root_type(self):
        schema = "type User @entity(name: \"users\", key: \"id\") { id: ID! }\n"
        self.assert_error(*self.run_cli("{ users { id } }", schema=schema),
                          "UnknownField")

    def test_scalar_field_with_selection(self):
        self.assert_error(*self.run_cli("{ users { id { x } } }"),
                          "InvalidQuery")

    def test_empty_selection(self):
        self.assert_error(*self.run_cli("query { users { } }"), "InvalidQuery")

    # -- variables errors ---------------------------------------------------------

    def test_variable_missing(self):
        self.assert_error(
            *self.run_cli("query ($id: ID!) { users(id: $id) { id } }"),
            "VariablesError",
        )

    def test_variable_wrong_type(self):
        self.assert_error(
            *self.run_cli("query ($id: ID) { users(id: $id) { id } }",
                          '{"id": {}}'),
            "VariablesError",
        )

    def test_variable_undeclared(self):
        self.assert_error(
            *self.run_cli("{ users(id: $id) { id } }", '{"id": 1}'),
            "VariablesError",
        )

    def test_variables_file_not_an_object(self):
        self.assert_error(*self.run_cli("{ users { id } }", "[1, 2]"),
                          "VariablesError")

    # -- event errors ------------------------------------------------------------------

    def _events_error(self, events, expected="EventError", query="{ users { id } }"):
        self.assert_error(*self.run_cli(query, events=events), expected)

    def test_event_invalid_json(self):
        self._events_error("{not json\n")

    def test_event_not_an_object(self):
        self._events_error("[1, 2]\n")

    def test_event_missing_key(self):
        self._events_error('{"op": "INSERT", "entity": "users", "after": {}}\n')

    def test_event_unknown_op(self):
        self._events_error(
            '{"op": "UPSERT", "entity": "users", "before": null, "after": {}}\n'
        )

    def test_event_snapshot_not_object(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": 5}\n'
        )

    def test_event_insert_without_snapshot(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": null}\n'
        )

    def test_event_delete_without_snapshot(self):
        self._events_error(
            '{"op": "DELETE", "entity": "users", "before": null, "after": null}\n'
        )

    def test_event_snapshot_missing_primary_key(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"name": "ada"}}\n'
        )

    def test_event_snapshot_null_primary_key(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": null, "name": "ada"}}\n'
        )

    def test_event_delete_before_missing_primary_key(self):
        self._events_error(
            '{"op": "DELETE", "entity": "users", "before": {"name": "ada"}, "after": null}\n'
        )

    def test_event_for_other_entity_with_null_snapshots_skipped(self):
        events = '{"op": "DELETE", "entity": "teams", "before": null, "after": null}\n'
        # teams is reachable only when selected; a users-only query ignores it
        data = self.assert_data(*self.run_cli("{ users { id } }",
                                              events=events))
        self.assertEqual(data, {"users": []})

    def test_event_snapshot_missing_selected_field(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1}}\n',
            query="{ users { id name } }",
        )

    def test_event_snapshot_missing_filter_field(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1}}\n',
            query='{ users(status: "active") { id } }',
        )

    # -- shape errors ------------------------------------------------------------

    def test_non_null_field_with_null_value(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1, "name": null}}\n',
            query="{ users { id name } }",
        )

    def test_list_field_with_scalar_value(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1, "tags": "a"}}\n',
            query="{ users { id tags } }",
        )

    def test_scalar_field_with_object_value(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1, "name": {"x": 1}}}\n',
            query="{ users { id name } }",
        )

    def test_list_field_with_null_item(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1, "tags": ["a", null]}}\n',
            query="{ users { id tags } }",
        )

    # -- parse / io errors --------------------------------------------------------------

    def test_query_parse_error(self):
        self.assert_error(*self.run_cli("{ users { id "), "ParseError")

    def test_variables_parse_error(self):
        self.assert_error(*self.run_cli("{ users { id } }", "{not json"),
                          "ParseError")

    def test_missing_events_file(self):
        schema_path = self._write("schema.graphql", SCHEMA)
        query_path = self._write("query.graphql", "{ users { id } }")
        variables_path = self._write("variables.json", "{}")
        argv = ["query-exec", "--schema", schema_path,
                "--query", query_path,
                "--variables", variables_path,
                "--events", "/nonexistent/events.ndjson"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")

    # -- nested @link projections --------------------------------------------------

    def test_nested_object_projected_from_final_snapshot(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "old"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
            _event("UPDATE", "teams",
                   {"id": 9, "name": "old"}, {"id": 9, "name": "new"}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id team { id name } } }",
            schema=NESTED_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [
            {"id": 1, "team": {"id": 9, "name": "new"}},
        ]})

    def test_nested_object_null_local_is_null_relationship(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "status": "active", "team_id": None},
        ) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id team { name } } }",
            schema=NESTED_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [{"id": 1, "team": None}]})

    def test_null_local_on_non_null_object_is_event_error(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "status": "active", "team_id": None},
        ) + "\n"
        self.assert_error(
            *self.run_cli("{ users { id group { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_missing_target_snapshot_is_event_error(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "status": "active", "team_id": 77},
        ) + "\n"
        self.assert_error(
            *self.run_cli("{ users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_deleted_target_snapshot_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
            _event("DELETE", "teams", {"id": 9, "name": "core"}, None),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("{ users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_multi_level_nested_links(self):
        events = "\n".join([
            _event("INSERT", "leagues", None, {"id": 5, "title": "L1"}),
            _event("INSERT", "teams", None,
                   {"id": 9, "name": "core", "league_id": 5}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id team { name league { title } } } }",
            schema=NESTED_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [
            {"id": 1, "team": {"name": "core", "league": {"title": "L1"}}},
        ]})

    def test_nested_target_not_primary_key_is_invalid_join(self):
        schema = COMPOSITE_SCHEMA.replace(
            'target: ["chain_id", "id"]', 'target: ["chain_id", "symbol"]'
        )
        self.assert_error(
            *self.run_cli("{ transfers { id token { symbol } } }",
                          schema=schema),
            "InvalidJoin",
        )

    # -- list @link projections ---------------------------------------------------

    def test_list_matches_targets_in_insert_order(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 7, "score": 2}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id reviews { id score } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [
            {"id": 7, "reviews": [
                {"id": "r1", "score": 1},
                {"id": "r2", "score": 2},
            ]},
        ]})

    def test_list_no_match_or_null_local_is_empty_array(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "org_id": None}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id reviews { id } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [
            {"id": 1, "reviews": []},
            {"id": 2, "reviews": []},
        ]})

    def test_list_delete_then_reinsert_goes_to_end(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 7, "score": 2}),
            _event("DELETE", "reviews",
                   {"id": "r1", "user_id": 7, "score": 1}, None),
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 11}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { reviews { id score } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [
            {"reviews": [
                {"id": "r2", "score": 2},
                {"id": "r1", "score": 11},
            ]},
        ]})

    def test_list_nested_inside_object_link(self):
        events = "\n".join([
            _event("INSERT", "orgs", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "org_id": 9}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ users { id org { name members { id } } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"users": [
            {"id": 1, "org": {"name": "core", "members": [{"id": 1}, {"id": 2}]}},
            {"id": 2, "org": {"name": "core", "members": [{"id": 1}, {"id": 2}]}},
        ]})

    # -- composite keys ---------------------------------------------------------

    def test_composite_key_query(self):
        events = "\n".join([
            _event("INSERT", "tokens", None,
                   {"chain_id": 1, "id": "t1", "symbol": "OLD"}),
            _event("UPDATE", "tokens",
                   {"chain_id": 1, "id": "t1", "symbol": "OLD"},
                   {"chain_id": 1, "id": "t1", "symbol": "NEW"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 1, "id": "x1",
                    "token_chain": 1, "token_id": "t1"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 2, "id": "x2",
                    "token_chain": None, "token_id": None}),
        ]) + "\n"
        query = "{ transfers { chain_id id token { symbol } } }"
        data = self.assert_data(
            *self.run_cli(query, schema=COMPOSITE_SCHEMA, events=events)
        )
        self.assertEqual(data, {"transfers": [
            {"chain_id": 1, "id": "x1", "token": {"symbol": "NEW"}},
            {"chain_id": 2, "id": "x2", "token": None},
        ]})

    def test_composite_key_filter(self):
        events = "\n".join([
            _event("INSERT", "transfers", None,
                   {"chain_id": 1, "id": "t1", "amount": 3.5}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 2, "id": "t2", "amount": 1.0}),
        ]) + "\n"
        data = self.assert_data(*self.run_cli(
            "{ transfers(chain_id: 1) { chain_id id } }",
            schema=COMPOSITE_SCHEMA, events=events,
        ))
        self.assertEqual(data, {"transfers": [{"chain_id": 1, "id": "t1"}]})


if __name__ == "__main__":
    unittest.main()
