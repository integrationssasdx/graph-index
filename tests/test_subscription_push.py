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

    def test_list_relation_unsupported(self):
        schema = SCHEMA + """
        extend type User { accounts: [Account!]! @link(local: "id", target: "id") }
        type Account @entity(name: "accounts", key: "id") { id: ID! }
        """
        self.assert_error(
            *self.run_cli("subscription { users { accounts { id } } }",
                          schema=schema),
            "UnsupportedSelection",
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

    # -- nested @link projection --------------------------------------------------

    LEAGUE_SCHEMA = SCHEMA + """
    extend type Team {
      league_id: ID
      league: League @link(local: "league_id", target: "id")
    }
    type League @entity(name: "leagues", key: "id") {
      id: ID!
      title: String!
    }
    """

    @staticmethod
    def _events(rows):
        return "\n".join(json.dumps(row) for row in rows) + "\n"

    def _user(self, uid, team_id=None, status="active", name=None):
        return {
            "op": "INSERT", "entity": "users", "before": None,
            "after": {"id": uid, "name": name or f"user{uid}", "status": status,
                      "role": "MEMBER", "tags": [], "team_id": team_id},
        }

    def test_nested_object_projects_nested_data(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9),
        ])
        subscription = "subscription { users { id team { name } } }"
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"], {"id": 1, "team": {"name": "core"}})

    def test_nested_fields_are_not_flattened(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9),
        ])
        subscription = "subscription { users { name team { id name } } }"
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(
            rows[0]["data"],
            {"name": "user1", "team": {"id": 9, "name": "core"}},
        )

    def test_nested_aliases_are_honored(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9),
        ])
        subscription = "subscription { users { who: name t: team { teamName: name } } }"
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(
            rows[0]["data"], {"who": "user1", "t": {"teamName": "core"}}
        )

    def test_nested_fragments_expanded_and_merged(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9),
        ])
        subscription = """
        subscription {
          users {
            id
            ...UserBits
            team { id ...TeamBits }
          }
        }
        fragment UserBits on User { name }
        fragment TeamBits on Team { name }
        """
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "name": "user1", "team": {"id": 9, "name": "core"}},
        )

    def test_nested_leaf_deduplicated(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9),
        ])
        subscription = """
        subscription { users { id team { name name } } }
        """
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(rows[0]["data"], {"id": 1, "team": {"name": "core"}})

    def test_nested_relation_resolves_from_latest_snapshot(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9, name="ada"),
            {"op": "UPDATE", "entity": "teams",
             "before": {"id": 9, "name": "core"},
             "after": {"id": 9, "name": "platform"}},
            self._user(2, 9, name="bob"),
        ])
        subscription = "subscription { users { name team { name } } }"
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(rows[0]["data"]["team"], {"name": "core"})
        self.assertEqual(rows[1]["data"]["team"], {"name": "platform"})

    def test_target_snapshot_seen_after_root_event_is_not_retroactive(self):
        events = self._events([
            self._user(1, 9),
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
        ])
        subscription = "subscription { users { id team { name } } }"
        self.assert_error(*self.run_cli(subscription, events=events), "EventError")

    def test_deleted_target_snapshot_cannot_resolve(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            {"op": "DELETE", "entity": "teams",
             "before": {"id": 9, "name": "core"}, "after": None},
            self._user(1, 9),
        ])
        subscription = "subscription { users { id team { name } } }"
        self.assert_error(*self.run_cli(subscription, events=events), "EventError")

    def test_null_local_resolves_to_nested_null(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, None),
        ])
        subscription = "subscription { users { id team { name } } }"
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual(rows[0]["data"], {"id": 1, "team": None})

    def test_null_local_on_non_null_relation_is_event_error(self):
        schema = SCHEMA.replace(
            "team: Team @link(local: \"team_id\", target: \"id\")",
            "team: Team! @link(local: \"team_id\", target: \"id\")",
        )
        events = self._events([
            self._user(1, None),
        ])
        subscription = "subscription { users { id team { name } } }"
        self.assert_error(
            *self.run_cli(subscription, schema=schema, events=events),
            "EventError",
        )

    def test_missing_local_field_on_root_snapshot_is_event_error(self):
        # team_id absent (not null) on the root snapshot
        user = self._user(1, 9)
        del user["after"]["team_id"]
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            user,
        ])
        subscription = "subscription { users { id team { name } } }"
        self.assert_error(*self.run_cli(subscription, events=events), "EventError")

    def test_missing_target_leaf_field_is_event_error(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9}},
            self._user(1, 9),
        ])
        subscription = "subscription { users { id team { name } } }"
        self.assert_error(*self.run_cli(subscription, events=events), "EventError")

    def test_root_filter_still_applies_with_nested_selection(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core"}},
            self._user(1, 9, status="active"),
            self._user(2, 9, status="banned"),
        ])
        subscription = (
            "subscription { users(status: \"active\") { id team { name } } }"
        )
        rows = self.assert_rows(*self.run_cli(subscription, events=events))
        self.assertEqual([r["data"]["id"] for r in rows], [1])
        self.assertEqual(rows[0]["data"], {"id": 1, "team": {"name": "core"}})

    def test_multi_level_nesting(self):
        events = self._events([
            {"op": "INSERT", "entity": "leagues", "before": None,
             "after": {"id": 7, "title": "major"}},
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core", "league_id": 7}},
            self._user(1, 9),
        ])
        subscription = (
            "subscription { users { id team { name league { title } } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=self.LEAGUE_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "team": {"name": "core", "league": {"title": "major"}}},
        )

    def test_multi_level_null_mid_chain_propagates_as_null(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"id": 9, "name": "core", "league_id": None}},
            self._user(1, 9),
        ])
        subscription = (
            "subscription { users { id team { name league { title } } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=self.LEAGUE_SCHEMA, events=events)
        )
        self.assertEqual(
            rows[0]["data"],
            {"id": 1, "team": {"name": "core", "league": None}},
        )

    def test_composite_key_nested_resolution(self):
        events = self._events([
            {"op": "INSERT", "entity": "tokens", "before": None,
             "after": {"chain_id": 1, "id": "tok", "symbol": "TKN"}},
            {"op": "INSERT", "entity": "transfers", "before": None,
             "after": {"chain_id": 1, "id": "t1", "amount": 3.5,
                       "token_chain": 1, "token_id": "tok"}},
        ])
        subscription = (
            "subscription { transfers(chain_id: 1) "
            "{ chain_id id token { symbol } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=COMPOSITE_SCHEMA, events=events)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["data"],
            {"chain_id": 1, "id": "t1", "token": {"symbol": "TKN"}},
        )

    def test_composite_key_partial_local_null_resolves_null(self):
        events = self._events([
            {"op": "INSERT", "entity": "transfers", "before": None,
             "after": {"chain_id": 1, "id": "t1",
                       "token_chain": None, "token_id": "tok"}},
        ])
        subscription = (
            "subscription { transfers { id token { symbol } } }"
        )
        rows = self.assert_rows(
            *self.run_cli(subscription, schema=COMPOSITE_SCHEMA, events=events)
        )
        self.assertEqual(rows[0]["data"], {"id": "t1", "token": None})

    def test_composite_key_missing_local_field_is_event_error(self):
        events = self._events([
            {"op": "INSERT", "entity": "transfers", "before": None,
             "after": {"chain_id": 1, "id": "t1", "token_id": "tok"}},
        ])
        subscription = (
            "subscription { transfers { id token { symbol } } }"
        )
        self.assert_error(
            *self.run_cli(subscription, schema=COMPOSITE_SCHEMA, events=events),
            "EventError",
        )

    def test_target_event_with_missing_primary_key_field_is_event_error(self):
        events = self._events([
            {"op": "INSERT", "entity": "teams", "before": None,
             "after": {"name": "core"}},
            self._user(1, 9),
        ])
        subscription = "subscription { users { id team { name } } }"
        self.assert_error(*self.run_cli(subscription, events=events), "EventError")

    # -- nested selection compile errors -------------------------------------------

    def test_nested_empty_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { team { } } }"),
            "InvalidQuery",
        )

    def test_unknown_nested_field(self):
        self.assert_error(
            *self.run_cli("subscription { users { team { nope } } }"),
            "InvalidQuery",
        )

    def test_nested_scalar_with_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { team { name { x } } } }"),
            "InvalidQuery",
        )

    def test_nested_object_without_selection(self):
        self.assert_error(
            *self.run_cli("subscription { users { id team } }"),
            "InvalidQuery",
        )

    def test_nested_arguments_rejected(self):
        self.assert_error(
            *self.run_cli("subscription { users { team(x: 1) { id } } }"),
            "InvalidQuery",
        )

    def test_nested_object_without_link_is_mapping_error(self):
        schema = SCHEMA + """
        extend type User { org: Org }
        type Org @entity(name: "orgs", key: "id") { id: ID! name: String }
        """
        self.assert_error(
            *self.run_cli("subscription { users { org { name } } }",
                          schema=schema),
            "MappingError",
        )

    def test_nested_target_not_entity_is_unknown_entity(self):
        schema = SCHEMA + """
        extend type User { profile: Profile @link(local: "team_id", target: "id") }
        type Profile { id: ID! label: String }
        """
        self.assert_error(
            *self.run_cli("subscription { users { profile { label } } }",
                          schema=schema),
            "UnknownEntity",
        )

    def test_nested_link_target_wrong_order_is_invalid_join(self):
        schema = COMPOSITE_SCHEMA.replace(
            'target: ["chain_id", "id"]', 'target: ["id", "chain_id"]'
        )
        subscription = "subscription { transfers { token { symbol } } }"
        self.assert_error(
            *self.run_cli(subscription, schema=schema), "InvalidJoin"
        )


if __name__ == "__main__":
    unittest.main()
