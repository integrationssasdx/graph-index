"""End-to-end tests for `graph-index subscription-push`."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from graph_index.cli import main

SCHEMA = """\
type Query {
  users: [User!]!
}

type Subscription {
  users(id: ID, status: String, role: Role): [User!]!
  user(id: ID!): User
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
            {"op": "UPDATE", "entity": "users",
             "before": {"id": 1, "name": "ada", "status": "active",
                        "role": "ADMIN", "tags": ["a", "b"], "team_id": 9},
             "after": {"id": 1, "name": "ada", "status": "banned",
                       "role": "ADMIN", "tags": ["a"], "team_id": 9}},
            {"op": "DELETE", "entity": "users",
             "before": {"id": 2, "name": "bob", "status": "active",
                        "role": "MEMBER", "tags": [], "team_id": None},
             "after": None},
        ]
    ]
) + "\n"

COMPOSITE_SCHEMA = """\
type Query {
  transfers: [Transfer!]!
}

type Subscription {
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

NESTED_SCHEMA = """\
type Query {
  users: [User!]!
}

type Subscription {
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
  users: [User!]!
}

type Subscription {
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

COMPOSITE_LIST_SCHEMA = """\
type Query {
  orders: [Order!]!
}

type Subscription {
  orders(chain: Int, ref: ID): [Order!]!
}

type Order @entity(name: "orders", key: "id") {
  id: ID!
  chain: Int
  ref: ID
  lines: [Line!]! @link(local: ["chain", "ref"], target: ["chain", "ref"])
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


NESTED_EVENTS = "\n".join([
    _event("INSERT", "leagues", None, {"id": 5, "title": "L1"}),
    _event("INSERT", "orgs", None, {"id": 9, "name": "the-org"}),
    _event("INSERT", "teams", None,
           {"id": 9, "name": "core", "league_id": 5}),
    _event("INSERT", "users", None,
           {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
    _event("UPDATE", "users",
           {"id": 1, "name": "ada", "team_id": 9},
           {"id": 1, "name": "ada", "status": "idle", "team_id": None}),
    _event("UPDATE", "users",
           {"id": 1, "name": "ada", "team_id": None},
           {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
    _event("DELETE", "users",
           {"id": 2, "name": "bob", "status": "active", "team_id": 9}, None),
]) + "\n"



class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content, mode="w"):
        path = os.path.join(self.tmp.name, name)
        with open(path, mode) as handle:
            handle.write(content)
        return path

    def run_cli(self, subscription, variables="{}", schema=SCHEMA, events=EVENTS,
                operation=None):
        schema_path = self._write("schema.graphql", schema)
        subscription_path = self._write("subscription.graphql", subscription)
        variables_path = self._write("variables.json", variables)
        events_path = self._write("events.ndjson", events)
        argv = ["subscription-push", "--schema", schema_path,
                "--subscription", subscription_path,
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

    def assert_rows(self, code, stdout, stderr):
        self.assertEqual(stderr, "")
        self.assertEqual(code, 0, stderr)
        return [json.loads(line) for line in stdout.splitlines()]

    # -- happy paths ---------------------------------------------------------

    def test_no_filter_collects_all_entity_events_in_order(self):
        rows = self.assert_rows(*self.run_cli("subscription { users { id name } }"))
        self.assertEqual([r["event"] for r in rows], ["INSERT", "UPDATE", "DELETE"])
        for row in rows:
            self.assertIsNone(row["subscription"])
            self.assertEqual(row["path"], "users")
            self.assertEqual(row["entity"], "users")
        self.assertEqual(rows[0]["data"], {"id": 1, "name": "ada"})
        self.assertEqual(rows[1]["data"], {"id": 1, "name": "ada"})
        self.assertEqual(rows[2]["data"], {"id": 2, "name": "bob"})

    def test_filter_with_variable_and_literal(self):
        subscription = """
        subscription Watch($status: String) {
          users(status: $status, role: ADMIN) { id }
        }
        """
        rows = self.assert_rows(*self.run_cli(subscription, '{"status": "active"}'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subscription"], "Watch")
        self.assertEqual(rows[0]["event"], "INSERT")
        self.assertEqual(rows[0]["data"], {"id": 1})

    def test_delete_uses_before_snapshot(self):
        rows = self.assert_rows(*self.run_cli("subscription { users(status: \"active\") { id } }"))
        self.assertEqual([r["event"] for r in rows], ["INSERT", "DELETE"])
        self.assertEqual(rows[1]["data"], {"id": 2})

    def test_alias_and_list_preserved(self):
        subscription = "subscription { me: users { userId: id tags } }"
        rows = self.assert_rows(*self.run_cli(subscription))
        self.assertEqual(rows[0]["path"], "me")
        self.assertEqual(rows[0]["data"], {"userId": 1, "tags": ["a", "b"]})

    def test_variable_default_used_when_not_provided(self):
        subscription = (
            "subscription ($status: String = \"banned\") "
            "{ users(status: $status) { id } }"
        )
        rows = self.assert_rows(*self.run_cli(subscription))
        self.assertEqual([r["event"] for r in rows], ["UPDATE"])

    def test_operation_selected_by_name(self):
        subscription = """
        query Ignore { users { id } }
        subscription A { users { id } }
        subscription B { teams { id } }
        """
        rows = self.assert_rows(*self.run_cli(subscription, operation="B"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subscription"], "B")
        self.assertEqual(rows[0]["entity"], "teams")
        self.assertEqual(rows[0]["data"], {"id": 9})

    def test_fragments_expanded(self):
        subscription = """
        subscription { users { id ...Bits } }
        fragment Bits on User { name }
        """
        rows = self.assert_rows(*self.run_cli(subscription))
        self.assertEqual(rows[0]["data"], {"id": 1, "name": "ada"})

    def test_no_matching_events_outputs_nothing(self):
        code, stdout, stderr = self.run_cli(
            "subscription { users(status: \"nope\") { id } }"
        )
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_blank_event_lines_skipped(self):
        events = "\n" + EVENTS.replace("\n", "\n\n")
        rows = self.assert_rows(*self.run_cli("subscription { users { id } }",
                                              events=events))
        self.assertEqual(len(rows), 3)

    # -- operation selection errors -------------------------------------------

    def test_non_subscription_operation_rejected(self):
        self.assert_error(
            *self.run_cli("query { users { id } }"), "InvalidQuery"
        )

    def test_multiple_operations_require_name(self):
        subscription = "subscription A { users { id } } subscription B { users { id } }"
        self.assert_error(*self.run_cli(subscription), "InvalidQuery")

    def test_operation_name_must_match_a_subscription(self):
        subscription = "query A { users { id } } subscription B { users { id } }"
        self.assert_error(*self.run_cli(subscription, operation="A"), "InvalidQuery")
        self.assert_error(*self.run_cli(subscription, operation="Nope"), "InvalidQuery")

    def test_multiple_root_fields_rejected(self):
        self.assert_error(
            *self.run_cli("subscription { users { id } teams { id } }"),
            "InvalidQuery",
        )

    # -- selection / filter errors ----------------------------------------------

    def test_empty_selection(self):
        self.assert_error(*self.run_cli("subscription { users { } }"), "InvalidQuery")

    def test_unknown_root_field(self):
        self.assert_error(*self.run_cli("subscription { nope { id } }"), "InvalidQuery")

    def test_unknown_leaf_field(self):
        self.assert_error(
            *self.run_cli("subscription { users { nope } }"), "InvalidQuery"
        )

    def test_leaf_with_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { id { x } } }"), "InvalidQuery"
        )

    def test_unknown_argument(self):
        self.assert_error(
            *self.run_cli("subscription { users(nope: 1) { id } }"), "InvalidQuery"
        )

    def test_argument_not_an_entity_field(self):
        schema = SCHEMA.replace("users(id: ID, status: String, role: Role)",
                                "users(id: ID, age: Int)")
        self.assert_error(
            *self.run_cli("subscription { users(age: 3) { id } }", schema=schema),
            "InvalidQuery",
        )

    def test_missing_required_argument(self):
        self.assert_error(
            *self.run_cli("subscription { user { id } }"), "InvalidQuery"
        )

    # -- mapping errors ------------------------------------------------------------

    def test_no_subscription_root_type(self):
        schema = "type Query { users: [User!]! }\n" \
                 "type User @entity(name: \"users\", key: \"id\") { id: ID! }\n"
        self.assert_error(
            *self.run_cli("subscription { users { id } }", schema=schema),
            "MappingError",
        )

    def test_root_return_type_not_entity(self):
        schema = SCHEMA + "extend type Subscription { version: Version }\n" \
                          "type Version { tag: String }\n"
        self.assert_error(
            *self.run_cli("subscription { version { tag } }", schema=schema),
            "MappingError",
        )

    # -- variables errors -------------------------------------------------------------

    def test_variable_missing(self):
        self.assert_error(
            *self.run_cli("subscription ($id: ID!) { users(id: $id) { id } }"),
            "VariablesError",
        )

    def test_variable_wrong_type(self):
        self.assert_error(
            *self.run_cli("subscription ($id: ID) { users(id: $id) { id } }",
                          '{"id": {}}'),
            "VariablesError",
        )

    def test_variable_undeclared(self):
        self.assert_error(
            *self.run_cli("subscription { users(id: $id) { id } }", '{"id": 1}'),
            "VariablesError",
        )

    def test_variables_file_not_an_object(self):
        self.assert_error(
            *self.run_cli("subscription { users { id } }", "[1, 2]"),
            "VariablesError",
        )

    # -- event errors ------------------------------------------------------------------

    def _events_error(self, events, expected="EventError"):
        self.assert_error(
            *self.run_cli("subscription { users { id } }", events=events), expected
        )

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

    def test_event_snapshot_missing_selected_field(self):
        self._events_error(
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"name": "ada"}}\n'
        )

    def test_event_snapshot_missing_filter_field(self):
        self.assert_error(
            *self.run_cli(
                "subscription { users(status: \"active\") { id } }",
                events='{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1}}\n',
            ),
            "EventError",
        )

    def test_event_for_other_entity_with_null_snapshots_skipped(self):
        events = '{"op": "DELETE", "entity": "teams", "before": null, "after": null}\n'
        code, stdout, stderr = self.run_cli("subscription { users { id } }",
                                            events=events)
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    # -- parse / io errors --------------------------------------------------------------

    def test_subscription_parse_error(self):
        self.assert_error(
            *self.run_cli("subscription { users { id "), "ParseError"
        )

    def test_variables_parse_error(self):
        self.assert_error(
            *self.run_cli("subscription { users { id } }", "{not json"), "ParseError"
        )

    def test_missing_events_file(self):
        schema_path = self._write("schema.graphql", SCHEMA)
        subscription_path = self._write("subscription.graphql",
                                        "subscription { users { id } }")
        variables_path = self._write("variables.json", "{}")
        argv = ["subscription-push", "--schema", schema_path,
                "--subscription", subscription_path,
                "--variables", variables_path,
                "--events", "/nonexistent/events.ndjson"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")

    # -- composite keys ---------------------------------------------------------

    def test_composite_key_schema_pushes_events(self):
        events = "\n".join(
            json.dumps(row)
            for row in [
                {"op": "INSERT", "entity": "transfers", "before": None,
                 "after": {"chain_id": 1, "id": "t1", "amount": 3.5}},
                {"op": "INSERT", "entity": "transfers", "before": None,
                 "after": {"chain_id": 2, "id": "t2", "amount": 1.0}},
            ]
        ) + "\n"
        subscription = "subscription { transfers(chain_id: 1) { chain_id id } }"
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=COMPOSITE_SCHEMA, events=events)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity"], "transfers")
        self.assertEqual(rows[0]["data"], {"chain_id": 1, "id": "t1"})

    def test_invalid_composite_mapping_produces_no_output(self):
        schema = COMPOSITE_SCHEMA.replace(
            'key: ["chain_id", "id"]', 'key: ["chain_id", "chain_id"]', 1
        )
        self.assert_error(
            *self.run_cli("subscription { transfers { id } }", schema=schema),
            "MappingError",
        )

    # -- nested @link projections --------------------------------------------------

    def test_nested_object_projected_from_latest_target_snapshot(self):
        subscription = "subscription { users { id team { id name } } }"
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=NESTED_SCHEMA, events=NESTED_EVENTS)
        )
        self.assertEqual([r["event"] for r in rows],
                         ["INSERT", "UPDATE", "UPDATE", "DELETE"])
        self.assertEqual(rows[0]["data"],
                         {"id": 1, "team": {"id": 9, "name": "core"}})
        # UPDATE set team_id to null: the relationship is null
        self.assertEqual(rows[1]["data"], {"id": 1, "team": None})
        # later UPDATE restored the link; DELETE projects from its before row
        self.assertEqual(rows[2]["data"],
                         {"id": 1, "team": {"id": 9, "name": "core"}})
        self.assertEqual(rows[3]["data"],
                         {"id": 2, "team": {"id": 9, "name": "core"}})

    def test_nested_object_with_alias_and_fragment(self):
        subscription = """
        subscription {
          users {
            id
            myTeam: team { teamId: id ...TeamBits }
          }
        }
        fragment TeamBits on Team { name }
        """
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=NESTED_SCHEMA, events=NESTED_EVENTS)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "myTeam": {"teamId": 9, "name": "core"}},
        )

    def test_multi_level_nested_links(self):
        subscription = (
            "subscription { users { id team { name league { title } } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=NESTED_SCHEMA, events=NESTED_EVENTS)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "team": {"name": "core", "league": {"title": "L1"}}},
        )
        self.assertEqual(
            rows[1]["data"],
            {"id": 1, "team": None},
        )

    def test_nested_selection_deduplicated_across_fragments(self):
        subscription = """
        subscription {
          users {
            id
            team { id }
            ...WithTeam
          }
        }
        fragment WithTeam on User { team { name } }
        """
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=NESTED_SCHEMA, events=NESTED_EVENTS)
        )
        self.assertEqual(
            rows[0]["data"], {"id": 1, "team": {"id": 9, "name": "core"}}
        )

    def test_target_update_is_observed_by_later_root_event(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "old"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("UPDATE", "teams",
                   {"id": 9, "name": "old"}, {"id": 9, "name": "new"}),
            _event("UPDATE", "users",
                   {"id": 1, "name": "ada", "team_id": 9},
                   {"id": 1, "name": "ada", "team_id": 9}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli("subscription { users { team { name } } }",
                          schema=NESTED_SCHEMA, events=events)
        )
        self.assertEqual(rows[0]["data"], {"team": {"name": "old"}})
        self.assertEqual(rows[1]["data"], {"team": {"name": "new"}})

    def test_target_delete_makes_later_relationship_fail(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("DELETE", "teams", {"id": 9, "name": "core"}, None),
            _event("UPDATE", "users",
                   {"id": 1, "name": "ada", "team_id": 9},
                   {"id": 1, "name": "ada", "team_id": 9}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_relationship_sees_only_snapshots_up_to_current_line(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_null_local_on_non_null_object_is_event_error(self):
        self.assert_error(
            *self.run_cli("subscription { users { id group { name } } }",
                          schema=NESTED_SCHEMA, events=NESTED_EVENTS),
            "EventError",
        )

    def test_root_snapshot_missing_local_key_field_is_event_error(self):
        events = _event(
            "INSERT", "users", None, {"id": 1, "name": "ada"}
        ) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_unbuildable_local_key_value_is_event_error(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "team_id": ["x"]},
        ) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_missing_target_snapshot_is_event_error(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "team_id": 77},
        ) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_target_snapshot_missing_selected_field_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=NESTED_SCHEMA, events=events),
            "EventError",
        )

    def test_composite_key_link_projection(self):
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
        subscription = (
            "subscription { transfers { chain_id id token { symbol } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=COMPOSITE_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"chain_id": 1, "id": "x1", "token": {"symbol": "NEW"}},
        )
        self.assertEqual(
            rows[1]["data"],
            {"chain_id": 2, "id": "x2", "token": None},
        )

    def test_composite_link_partial_local_null_is_null_relationship(self):
        events = _event(
            "INSERT", "transfers", None,
            {"chain_id": 1, "id": "x1", "token_chain": None, "token_id": "t1"},
        ) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { transfers { id token { symbol } } }",
                schema=COMPOSITE_SCHEMA, events=events,
            )
        )
        self.assertEqual(rows[0]["data"], {"id": "x1", "token": None})

    # -- list @link projections ---------------------------------------------------

    def test_list_matches_targets_and_preserves_insert_order(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 7, "score": 2}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        subscription = (
            "subscription { users { id reviews { id score } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=LIST_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 7, "reviews": [
                {"id": "r1", "score": 1},
                {"id": "r2", "score": 2},
            ]},
        )

    def test_list_target_may_match_non_primary_key_field(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 3, "name": "cy", "org_id": 4}),
        ]) + "\n"
        subscription = "subscription { users { id mates { id } } }"
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=LIST_SCHEMA, events=events)
        )
        # Only targets inserted up to and including the current line match.
        self.assertEqual(
            [r["data"] for r in rows],
            [
                {"id": 1, "mates": [{"id": 1}]},
                {"id": 2, "mates": [{"id": 1}, {"id": 2}]},
                {"id": 3, "mates": [{"id": 3}]},
            ],
        )

    def test_list_no_match_or_null_local_is_empty_array(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "org_id": None}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id reviews { id } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        self.assertEqual(
            [r["data"] for r in rows],
            [{"id": 1, "reviews": []}, {"id": 2, "reviews": []}],
        )

    def test_list_update_preserves_position(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 7, "score": 2}),
            _event("UPDATE", "reviews",
                   {"id": "r1", "user_id": 7, "score": 1},
                   {"id": "r1", "user_id": 7, "score": 10}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { reviews { id score } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        self.assertEqual(
            rows[0]["data"]["reviews"],
            [{"id": "r1", "score": 10}, {"id": "r2", "score": 2}],
        )

    def test_list_target_enter_leave_reenter_group(self):
        # An UPDATE that changes the matched field moves a row out of and into
        # a relationship; position is still governed by its original insert.
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 7, "score": 2}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
            _event("UPDATE", "reviews",
                   {"id": "r1", "user_id": 7, "score": 1},
                   {"id": "r1", "user_id": 8, "score": 1}),
            _event("UPDATE", "users",
                   {"id": 7, "name": "grace", "org_id": 1},
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { reviews { id } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        self.assertEqual(rows[0]["data"]["reviews"], [
            {"id": "r1"}, {"id": "r2"},
        ])
        self.assertEqual(rows[1]["data"]["reviews"], [{"id": "r2"}])

    def test_list_delete_then_reinsert_goes_to_end(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 7, "score": 2}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
            _event("DELETE", "reviews",
                   {"id": "r1", "user_id": 7, "score": 1}, None),
            _event("UPDATE", "users",
                   {"id": 7, "name": "grace", "org_id": 1},
                   {"id": 7, "name": "grace", "org_id": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 11}),
            _event("UPDATE", "users",
                   {"id": 7, "name": "grace", "org_id": 1},
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { reviews { id score } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        # The root INSERT and the two root UPDATEs frame synthetic dependency
        # UPDATEs emitted right after the review DELETE and re-INSERT.
        self.assertEqual([r["event"] for r in rows],
                         ["INSERT", "UPDATE", "UPDATE", "UPDATE", "UPDATE"])
        self.assertTrue(all(r["entity"] == "users" for r in rows))
        self.assertEqual([r["path"] for r in rows], ["users"] * 5)
        self.assertEqual(rows[0]["data"]["reviews"], [
            {"id": "r1", "score": 1}, {"id": "r2", "score": 2},
        ])
        # dependency record after the DELETE
        self.assertEqual(rows[1]["data"]["reviews"], [
            {"id": "r2", "score": 2},
        ])
        self.assertEqual(rows[2]["data"]["reviews"], [
            {"id": "r2", "score": 2},
        ])
        # re-INSERTed r1 sorts after r2 even though it first appeared earlier;
        # the dependency record after that INSERT already shows the final order
        self.assertEqual(rows[3]["data"]["reviews"], [
            {"id": "r2", "score": 2}, {"id": "r1", "score": 11},
        ])
        self.assertEqual(rows[4]["data"]["reviews"], [
            {"id": "r2", "score": 2}, {"id": "r1", "score": 11},
        ])

    def test_list_at_root_alias_and_fragment_preserved(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 5}),
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
        ]) + "\n"
        subscription = """
        subscription {
          users {
            id
            myReviews: reviews { reviewId: id ...ScoreBit }
          }
        }
        fragment ScoreBit on Review { score }
        """
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=LIST_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 7, "myReviews": [{"reviewId": "r1", "score": 5}]},
        )

    def test_list_nested_inside_object_link(self):
        events = "\n".join([
            _event("INSERT", "orgs", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "org_id": 9}),
        ]) + "\n"
        subscription = (
            "subscription { users { id org { name members { id } } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=LIST_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "org": {"name": "core", "members": [{"id": 1}]}},
        )
        self.assertEqual(
            rows[1]["data"],
            {"id": 2, "org": {"name": "core", "members": [
                {"id": 1}, {"id": 2},
            ]}},
        )

    def test_list_field_alongside_object_link_recurses(self):
        events = "\n".join([
            _event("INSERT", "orgs", None, {"id": 9, "name": "core"}),
            _event("INSERT", "reviews", None,
                   {"id": "x", "user_id": 1, "score": 3}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
        ]) + "\n"
        subscription = (
            "subscription { users { id org { name } reviews { score } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=LIST_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 1,
             "org": {"name": "core"},
             "reviews": [{"score": 3}]},
        )

    def test_composite_non_primary_key_list_match(self):
        events = "\n".join([
            _event("INSERT", "lines", None,
                   {"id": "a", "chain": 1, "ref": "R", "qty": 2}),
            _event("INSERT", "lines", None,
                   {"id": "b", "chain": 1, "ref": "R", "qty": 5}),
            _event("INSERT", "lines", None,
                   {"id": "c", "chain": 2, "ref": "R", "qty": 9}),
            _event("INSERT", "orders", None,
                   {"id": "o1", "chain": 1, "ref": "R"}),
        ]) + "\n"
        subscription = (
            "subscription { orders(chain: 1) { id lines { id qty } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(
                subscription, schema=COMPOSITE_LIST_SCHEMA, events=events
            )
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": "o1", "lines": [
                {"id": "a", "qty": 2}, {"id": "b", "qty": 5},
            ]},
        )

    def test_list_snapshot_missing_local_field_is_event_error(self):
        events = _event(
            "INSERT", "users", None, {"id": 1, "name": "ada"}
        ) + "\n"
        self.assert_error(
            *self.run_cli(
                "subscription { users { id mates { id } } }",
                schema=LIST_SCHEMA, events=events,
            ),
            "EventError",
        )

    def test_list_unbuildable_local_value_is_event_error(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "org_id": {"x": 1}},
        ) + "\n"
        self.assert_error(
            *self.run_cli(
                "subscription { users { id mates { id } } }",
                schema=LIST_SCHEMA, events=events,
            ),
            "EventError",
        )

    def test_list_target_snapshot_missing_match_field_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "reviews", None, {"id": "r1", "score": 1}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 1}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli(
                "subscription { users { id reviews { score } } }",
                schema=LIST_SCHEMA, events=events,
            ),
            "EventError",
        )

    def test_list_target_snapshot_missing_selected_field_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 1}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 1}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli(
                "subscription { users { id reviews { score } } }",
                schema=LIST_SCHEMA, events=events,
            ),
            "EventError",
        )

    def test_list_target_field_non_scalar_is_mapping_error(self):
        # A list target field exists on the type (so schema loading accepts
        # it) but cannot serve as an equality-match key for a list @link.
        schema = LIST_SCHEMA + (
            "extend type Review { tags: [String!] }\n"
            "extend type User {"
            " tagged: [Review!]! @link(local: \"id\", target: \"tags\")}\n"
        )
        self.assert_error(
            *self.run_cli(
                "subscription { users { id tagged { id } } }",
                schema=schema,
            ),
            "MappingError",
        )

    def test_list_target_field_missing_is_mapping_error(self):
        schema = LIST_SCHEMA + (
            "extend type User {"
            " weird: [Review!]! @link(local: \"id\", target: \"nope\")}\n"
        )
        self.assert_error(
            *self.run_cli(
                "subscription { users { id weird { id } } }",
                schema=schema,
            ),
            "MappingError",
        )

    def test_list_local_field_list_wrapped_scalar_is_mapping_error(self):
        schema = LIST_SCHEMA + (
            "extend type User { codes: [String!] }\n"
            "extend type User {"
            " coded: [Review!]! @link(local: \"codes\", target: \"score\")}\n"
        )
        self.assert_error(
            *self.run_cli(
                "subscription { users { id coded { id } } }",
                schema=schema,
            ),
            "MappingError",
        )

    def test_list_matched_target_missing_primary_key_is_event_error(self):
        # A review row that cannot build its primary key still participates in
        # target matching and must surface as EventError once it matches.
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"user_id": 1, "score": 5}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 1}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli(
                "subscription { users { id reviews { score } } }",
                schema=LIST_SCHEMA, events=events,
            ),
            "EventError",
        )

    def test_list_link_to_non_entity_target_is_unknown_entity(self):
        schema = LIST_SCHEMA + (
            "type Note { id: ID! }\n"
            "extend type User {"
            " notes: [Note!]! @link(local: \"id\", target: \"id\")}\n"
        )
        self.assert_error(
            *self.run_cli(
                "subscription { users { id notes { id } } }",
                schema=schema,
            ),
            "UnknownEntity",
        )

    # -- dependency change push --------------------------------------------------

    def test_dependency_object_target_update_appends_root_update(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
            _event("UPDATE", "teams",
                   {"id": 9, "name": "core"}, {"id": 9, "name": "platform"}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id team { name } } }",
                schema=NESTED_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows], ["INSERT", "UPDATE"])
        self.assertEqual([r["entity"] for r in rows], ["users", "users"])
        self.assertEqual([r["path"] for r in rows], ["users", "users"])
        self.assertEqual(rows[0]["data"], {"id": 1, "team": {"name": "core"}})
        self.assertEqual(rows[1]["data"],
                         {"id": 1, "team": {"name": "platform"}})

    def test_dependency_record_uses_subscription_name_and_path(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("UPDATE", "teams",
                   {"id": 9, "name": "core"}, {"id": 9, "name": "platform"}),
        ]) + "\n"
        subscription = """
        subscription WatchTeam {
          watched: users { myTeam: team { name } }
        }
        """
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=NESTED_SCHEMA, events=events)
        )
        self.assertEqual(len(rows), 2)
        record = rows[1]
        self.assertEqual(record["subscription"], "WatchTeam")
        self.assertEqual(record["path"], "watched")
        self.assertEqual(record["event"], "UPDATE")
        self.assertEqual(record["entity"], "users")
        self.assertEqual(record["data"],
                         {"myTeam": {"name": "platform"}})

    def test_dependency_change_to_unselected_field_makes_no_noise(self):
        events = "\n".join([
            _event("INSERT", "teams", None,
                   {"id": 9, "name": "core", "league_id": 5}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            # league_id is not projected through team, so changing it does not
            # alter the selected projection
            _event("UPDATE", "teams",
                   {"id": 9, "name": "core", "league_id": 5},
                   {"id": 9, "name": "core", "league_id": 6}),
            # an in-place UPDATE with identical content also makes no noise
            _event("UPDATE", "teams",
                   {"id": 9, "name": "core", "league_id": 6},
                   {"id": 9, "name": "core", "league_id": 6}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id team { name } } }",
                schema=NESTED_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows], ["INSERT"])

    def test_dependency_deep_link_change_propagates(self):
        events = "\n".join([
            _event("INSERT", "leagues", None, {"id": 5, "title": "L1"}),
            _event("INSERT", "teams", None,
                   {"id": 9, "name": "core", "league_id": 5}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("UPDATE", "leagues",
                   {"id": 5, "title": "L1"}, {"id": 5, "title": "L2"}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id team { name league { title } } } }",
                schema=NESTED_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows], ["INSERT", "UPDATE"])
        self.assertEqual(rows[1]["data"],
                         {"id": 1, "team": {
                             "name": "core", "league": {"title": "L2"}}})

    def test_dependency_null_object_relationship_stays_null(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": None}),
            # inserting a target cannot resolve a relationship whose local key
            # is null: no projection change, no record
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("UPDATE", "teams",
                   {"id": 9, "name": "core"}, {"id": 9, "name": "x"}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id team { name } } }",
                schema=NESTED_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows], ["INSERT"])
        self.assertEqual(rows[0]["data"], {"id": 1, "team": None})

    def test_dependency_event_on_unreachable_entity_ignored(self):
        events = "\n".join([
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 1, "score": 5}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            # orgs is reachable in the schema but not from this selection tree
            _event("INSERT", "orgs", None, {"id": 9, "name": "core"}),
            _event("UPDATE", "orgs",
                   {"id": 9, "name": "core"}, {"id": 9, "name": "other"}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id reviews { score } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows], ["INSERT"])

    def test_dependency_only_pushed_for_roots_still_matching_filter(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "status": "banned", "team_id": 9}),
            # banned root is not currently matching: the target change is silent
            _event("UPDATE", "teams",
                   {"id": 9, "name": "core"}, {"id": 9, "name": "platform"}),
            # the root's own UPDATE brings it into the filter
            _event("UPDATE", "users",
                   {"id": 1, "name": "ada", "status": "banned", "team_id": 9},
                   {"id": 1, "name": "ada", "status": "active", "team_id": 9}),
            # now a target change reaches it
            _event("UPDATE", "teams",
                   {"id": 9, "name": "platform"}, {"id": 9, "name": "core"}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                'subscription { users(status: "active") { id team { name } } }',
                schema=NESTED_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows], ["UPDATE", "UPDATE"])
        self.assertEqual(rows[0]["data"],
                         {"id": 1, "team": {"name": "platform"}})
        self.assertEqual(rows[1]["data"],
                         {"id": 1, "team": {"name": "core"}})

    def test_dependency_list_target_insert_update_delete(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 7, "name": "grace", "org_id": 1}),
            _event("INSERT", "reviews", None,
                   {"id": "r1", "user_id": 7, "score": 1}),
            _event("UPDATE", "reviews",
                   {"id": "r1", "user_id": 7, "score": 1},
                   {"id": "r1", "user_id": 7, "score": 10}),
            _event("UPDATE", "reviews",
                   {"id": "r1", "user_id": 7, "score": 10},
                   {"id": "r1", "user_id": 8, "score": 10}),
            # the review already left the group, so its DELETE changes nothing
            _event("DELETE", "reviews",
                   {"id": "r1", "user_id": 8, "score": 10}, None),
            # a review for another user never affects the root projection
            _event("INSERT", "reviews", None,
                   {"id": "r2", "user_id": 99, "score": 4}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id reviews { id score } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows],
                         ["INSERT", "UPDATE", "UPDATE", "UPDATE"])
        self.assertTrue(all(r["entity"] == "users" for r in rows))
        self.assertEqual(rows[0]["data"]["reviews"], [])
        self.assertEqual(rows[1]["data"]["reviews"],
                         [{"id": "r1", "score": 1}])
        self.assertEqual(rows[2]["data"]["reviews"],
                         [{"id": "r1", "score": 10}])
        self.assertEqual(rows[3]["data"]["reviews"], [])

    def test_dependency_multiple_roots_follow_stable_insert_order(self):
        events = "\n".join([
            _event("INSERT", "orders", None,
                   {"id": "o1", "chain": 1, "ref": "R"}),
            _event("INSERT", "orders", None,
                   {"id": "o2", "chain": 1, "ref": "R"}),
            _event("INSERT", "orders", None,
                   {"id": "o3", "chain": 2, "ref": "R"}),
            _event("INSERT", "lines", None,
                   {"id": "a", "chain": 1, "ref": "R", "qty": 2}),
        ]) + "\n"
        subscription = (
            "subscription { orders(chain: 1) { id lines { id qty } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(
                subscription, schema=COMPOSITE_LIST_SCHEMA, events=events
            )
        )
        self.assertEqual([r["event"] for r in rows],
                         ["INSERT", "INSERT", "UPDATE", "UPDATE"])
        # the chain-2 order is filtered out and never appears
        self.assertEqual([r["data"]["id"] for r in rows[:2]], ["o1", "o2"])
        self.assertEqual(rows[2]["data"]["id"], "o1")
        self.assertEqual(rows[3]["data"]["id"], "o2")
        self.assertEqual(rows[2]["data"]["lines"], [{"id": "a", "qty": 2}])
        self.assertEqual(rows[3]["data"]["lines"], [{"id": "a", "qty": 2}])

    def test_dependency_order_reattaches_after_root_reinsert(self):
        events = "\n".join([
            _event("INSERT", "orders", None,
                   {"id": "o1", "chain": 1, "ref": "R"}),
            _event("INSERT", "orders", None,
                   {"id": "o2", "chain": 1, "ref": "R"}),
            _event("DELETE", "orders",
                   {"id": "o1", "chain": 1, "ref": "R"}, None),
            _event("INSERT", "orders", None,
                   {"id": "o1", "chain": 1, "ref": "R"}),
            _event("INSERT", "lines", None,
                   {"id": "a", "chain": 1, "ref": "R", "qty": 2}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { orders(chain: 1) { id lines { id } } }",
                schema=COMPOSITE_LIST_SCHEMA, events=events,
            )
        )
        # the re-INSERTed root sorts last, so its dependency record does too
        self.assertEqual([r["event"] for r in rows],
                         ["INSERT", "INSERT", "DELETE", "INSERT",
                          "UPDATE", "UPDATE"])
        self.assertEqual([r["data"]["id"] for r in rows[-2:]], ["o2", "o1"])

    def test_dependency_target_delete_missing_object_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("DELETE", "teams", {"id": 9, "name": "core"}, None),
        ]) + "\n"
        code, stdout, stderr = self.run_cli(
            "subscription { users { id team { name } } }",
            schema=NESTED_SCHEMA, events=events,
        )
        self.assert_error(code, stdout, stderr, "EventError")

    def test_dependency_malformed_target_snapshot_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 1}),
            # the new review lacks the list @link match field user_id; the
            # dependency sweep must reject it instead of emitting partial rows
            _event("INSERT", "reviews", None, {"id": "r9", "score": 5}),
        ]) + "\n"
        code, stdout, stderr = self.run_cli(
            "subscription { users { id reviews { score } } }",
            schema=LIST_SCHEMA, events=events,
        )
        self.assert_error(code, stdout, stderr, "EventError")

    def test_root_table_events_never_get_synthetic_dependency_records(self):
        # mates is a list @link from users back into users: even though a users
        # event changes another root's selected projection, the changing entity
        # is the root table itself and only the event's own records are emitted
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "org_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "org_id": 9}),
            _event("UPDATE", "users",
                   {"id": 1, "name": "ada", "org_id": 9},
                   {"id": 1, "name": "ada", "org_id": 9}),
        ]) + "\n"
        rows = self.assert_rows(
            *self.run_cli(
                "subscription { users { id mates { id } } }",
                schema=LIST_SCHEMA, events=events,
            )
        )
        self.assertEqual([r["event"] for r in rows],
                         ["INSERT", "INSERT", "UPDATE"])
        self.assertEqual([r["data"]["id"] for r in rows], [1, 2, 1])

    # -- nested selection compile errors -------------------------------------------

    def test_nested_object_without_link_is_mapping_error(self):
        schema = NESTED_SCHEMA.replace(
            'team: Team @link(local: "team_id", target: "id")', "team: Team"
        )
        self.assert_error(
            *self.run_cli("subscription { users { id team { name } } }",
                          schema=schema),
            "MappingError",
        )

    def test_nested_target_not_entity_is_unknown_entity(self):
        schema = NESTED_SCHEMA + (
            "extend type User { profile: Profile "
            '@link(local: "team_id", target: "id") }\n'
            "type Profile { id: ID! }\n"
        )
        self.assert_error(
            *self.run_cli("subscription { users { id profile { id } } }",
                          schema=schema),
            "UnknownEntity",
        )

    def test_nested_target_not_primary_key_is_invalid_join(self):
        schema = COMPOSITE_SCHEMA.replace(
            'target: ["chain_id", "id"]', 'target: ["chain_id", "symbol"]'
        )
        self.assert_error(
            *self.run_cli(
                "subscription { transfers { id token { symbol } } }",
                schema=schema,
            ),
            "InvalidJoin",
        )

    def test_empty_nested_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { id team { } } }",
                          schema=NESTED_SCHEMA),
            "InvalidQuery",
        )

    def test_unknown_nested_field(self):
        self.assert_error(
            *self.run_cli("subscription { users { id team { nope } } }",
                          schema=NESTED_SCHEMA),
            "InvalidQuery",
        )

    def test_nested_scalar_field_with_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { id team { name { x } } } }",
                          schema=NESTED_SCHEMA),
            "InvalidQuery",
        )

    def test_nested_object_field_without_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { id team } }",
                          schema=NESTED_SCHEMA),
            "InvalidQuery",
        )

    def test_nested_field_arguments_rejected(self):
        self.assert_error(
            *self.run_cli("subscription { users { id team(x: 1) { name } } }",
                          schema=NESTED_SCHEMA),
            "InvalidQuery",
        )


if __name__ == "__main__":
    unittest.main()
