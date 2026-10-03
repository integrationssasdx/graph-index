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

    def test_nested_object_unsupported(self):
        self.assert_error(
            *self.run_cli("subscription { users { team { id } } }"),
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


if __name__ == "__main__":
    unittest.main()
