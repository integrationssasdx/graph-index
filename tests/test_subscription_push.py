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


LIST_SCHEMA = """\
type Query {
  users: [User!]!
}

type Subscription {
  users: [User!]!
  teams: [Team!]!
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  mates: [User!]! @link(local: "team_id", target: "team_id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
  members: [User!]! @link(local: "id", target: "team_id")
}
"""

LIST_EVENTS = "\n".join([
    _event("INSERT", "users", None, {"id": 1, "name": "ada", "team_id": 9}),
    _event("INSERT", "users", None, {"id": 2, "name": "bob", "team_id": 9}),
    _event("INSERT", "users", None, {"id": 3, "name": "cy", "team_id": 7}),
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

    # -- list relationships ------------------------------------------------------

    def test_list_relationship_matches_current_snapshots(self):
        rows = self.assert_rows(*self.run_cli(
            "subscription { users { id mates { id name } } }",
            schema=LIST_SCHEMA, events=LIST_EVENTS,
        ))
        self.assertEqual([r["event"] for r in rows], ["INSERT"] * 3)
        # the target of a list link need not be the target primary key
        self.assertEqual(rows[0]["data"],
                         {"id": 1, "mates": [{"id": 1, "name": "ada"}]})
        self.assertEqual(rows[1]["data"],
                         {"id": 2, "mates": [{"id": 1, "name": "ada"},
                                             {"id": 2, "name": "bob"}]})
        self.assertEqual(rows[2]["data"],
                         {"id": 3, "mates": [{"id": 3, "name": "cy"}]})

    def test_list_relationship_empty_when_nothing_matches(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 7}),
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
        ]) + "\n"
        rows = self.assert_rows(*self.run_cli(
            "subscription { teams { id members { id } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(rows[0]["data"], {"id": 9, "members": []})

    def test_list_relationship_empty_when_local_is_null(self):
        events = _event(
            "INSERT", "users", None, {"id": 1, "name": "ada", "team_id": None}
        ) + "\n"
        rows = self.assert_rows(*self.run_cli(
            "subscription { users { id mates { id } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(rows[0]["data"], {"id": 1, "mates": []})

    def test_list_relationship_ordering_rules(self):
        events = "\n".join([
            _event("INSERT", "users", None, {"id": 1, "name": "a", "team_id": 9}),
            _event("INSERT", "users", None, {"id": 2, "name": "b", "team_id": 9}),
            _event("INSERT", "users", None, {"id": 3, "name": "c", "team_id": 9}),
            _event("UPDATE", "users",
                   {"id": 2, "name": "b", "team_id": 9},
                   {"id": 2, "name": "b2", "team_id": 9}),
            _event("DELETE", "users", {"id": 1, "name": "a", "team_id": 9}, None),
            _event("INSERT", "users", None, {"id": 1, "name": "a2", "team_id": 9}),
        ]) + "\n"
        rows = self.assert_rows(*self.run_cli(
            "subscription { users { id mates { id } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        mate_ids = [[m["id"] for m in row["data"]["mates"]] for row in rows]
        # UPDATE keeps the target's position
        self.assertEqual(mate_ids[3], [1, 2, 3])
        # DELETE removes the target
        self.assertEqual(mate_ids[4], [2, 3])
        # re-INSERT after DELETE sorts last
        self.assertEqual(mate_ids[5], [2, 3, 1])

    def test_list_relationship_with_alias_and_fragment(self):
        subscription = """
        subscription {
          users { id pals: mates { mateId: id ...UserBits } }
        }
        fragment UserBits on User { name }
        """
        rows = self.assert_rows(*self.run_cli(
            subscription, schema=LIST_SCHEMA, events=LIST_EVENTS
        ))
        self.assertEqual(rows[0]["data"],
                         {"id": 1, "pals": [{"mateId": 1, "name": "ada"}]})

    def test_list_relationship_nested_under_object(self):
        events = "\n".join([
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "ada", "team_id": 9}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "bob", "team_id": 9}),
        ]) + "\n"
        rows = self.assert_rows(*self.run_cli(
            "subscription { users { id team { name members { id } } } }",
            schema=LIST_SCHEMA, events=events,
        ))
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "team": {"name": "core", "members": [{"id": 1}]}},
        )
        self.assertEqual(
            rows[1]["data"],
            {"id": 2, "team": {"name": "core",
                               "members": [{"id": 1}, {"id": 2}]}},
        )

    def test_list_relationship_with_composite_link(self):
        schema = COMPOSITE_SCHEMA + (
            "extend type Token { transfers: [Transfer!]! "
            '@link(local: ["chain_id", "id"], '
            'target: ["token_chain", "token_id"]) }\n'
        )
        events = "\n".join([
            _event("INSERT", "tokens", None,
                   {"chain_id": 1, "id": "t1", "symbol": "T"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 1, "id": "x1",
                    "token_chain": 1, "token_id": "t1"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 2, "id": "x2",
                    "token_chain": 1, "token_id": "t1"}),
            _event("INSERT", "transfers", None,
                   {"chain_id": 3, "id": "x3",
                    "token_chain": None, "token_id": None}),
        ]) + "\n"
        subscription = (
            "subscription { transfers { id token { symbol transfers { id } } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=schema, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": "x1",
             "token": {"symbol": "T", "transfers": [{"id": "x1"}]}},
        )
        self.assertEqual(
            rows[1]["data"],
            {"id": "x2",
             "token": {"symbol": "T", "transfers": [{"id": "x1"}, {"id": "x2"}]}},
        )
        self.assertEqual(rows[2]["data"], {"id": "x3", "token": None})

    def test_list_local_field_missing_is_event_error(self):
        events = _event("INSERT", "users", None, {"id": 1, "name": "ada"}) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id mates { id } } }",
                          schema=LIST_SCHEMA, events=events),
            "EventError",
        )

    def test_list_local_non_scalar_is_event_error(self):
        events = _event(
            "INSERT", "users", None,
            {"id": 1, "name": "ada", "team_id": ["x"]},
        ) + "\n"
        self.assert_error(
            *self.run_cli("subscription { users { id mates { id } } }",
                          schema=LIST_SCHEMA, events=events),
            "EventError",
        )

    def test_list_target_snapshot_missing_target_field_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "users", None, {"id": 1, "name": "ada"}),
            _event("INSERT", "teams", None, {"id": 9, "name": "core"}),
        ]) + "\n"
        self.assert_error(
            *self.run_cli("subscription { teams { id members { id } } }",
                          schema=LIST_SCHEMA, events=events),
            "EventError",
        )

    def test_list_link_missing_link_is_mapping_error(self):
        schema = LIST_SCHEMA.replace(
            ' mates: [User!]! @link(local: "team_id", target: "team_id")',
            " mates: [User!]!",
        )
        self.assert_error(
            *self.run_cli("subscription { users { id mates { id } } }",
                          schema=schema),
            "MappingError",
        )

    def test_list_target_not_entity_is_unknown_entity(self):
        schema = LIST_SCHEMA + (
            'extend type User { profiles: [Profile!]! '
            '@link(local: "team_id", target: "id") }\n'
            "type Profile { id: ID! }\n"
        )
        self.assert_error(
            *self.run_cli("subscription { users { id profiles { id } } }",
                          schema=schema),
            "UnknownEntity",
        )

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
