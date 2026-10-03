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
  users(id: ID, name: String, status: Status): [User!]!
  teams: [Team!]!
}

enum Status {
  ACTIVE
  BANNED
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: Status!
  tags: [String!]
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
}
"""

SUBSCRIPTION = """\
subscription WatchUsers($id: ID) {
  users(id: $id) {
    id
    name
    status
  }
}
"""

EVENTS = """\
{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1, "name": "ada", "status": "ACTIVE"}}
{"op": "UPDATE", "entity": "users", "before": {"id": 1, "name": "ada", "status": "ACTIVE"}, "after": {"id": 1, "name": "ada", "status": "BANNED"}}
{"op": "DELETE", "entity": "users", "before": {"id": 2, "name": "bob", "status": "ACTIVE"}, "after": null}
{"op": "INSERT", "entity": "teams", "before": null, "after": {"id": 9, "name": "core"}}
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

    def run_cli(self, subscription=SUBSCRIPTION, variables='{"id": 1}',
                events=EVENTS, schema=SCHEMA, operation=None):
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

    def assert_events(self, code, stdout, stderr):
        self.assertEqual(stderr, "")
        self.assertEqual(code, 0, stderr)
        return [json.loads(line) for line in stdout.splitlines()]

    # -- happy paths ---------------------------------------------------------

    def test_insert_update_matched_and_filtered(self):
        events = self.assert_events(*self.run_cli())
        self.assertEqual(len(events), 2)
        self.assertEqual(
            events[0],
            {"subscription": "WatchUsers", "path": "users", "event": "INSERT",
             "entity": "users",
             "data": {"id": 1, "name": "ada", "status": "ACTIVE"}},
        )
        self.assertEqual(events[1]["event"], "UPDATE")
        self.assertEqual(events[1]["data"]["status"], "BANNED")

    def test_delete_uses_before_snapshot(self):
        events = self.assert_events(*self.run_cli(variables='{"id": 2}'))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "DELETE")
        self.assertEqual(events[0]["data"]["name"], "bob")

    def test_no_arguments_matches_everything(self):
        events = self.assert_events(
            *self.run_cli(subscription="subscription { users { id name } }")
        )
        self.assertEqual([e["event"] for e in events],
                         ["INSERT", "UPDATE", "DELETE"])
        self.assertIsNone(events[0]["subscription"])

    def test_alias_and_literal_enum_filter(self):
        subscription = """
        subscription {
          active: users(status: ACTIVE) { userId: id name }
        }
        """
        events = self.assert_events(*self.run_cli(subscription=subscription))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["path"], "active")
        self.assertEqual(events[0]["data"], {"userId": 1, "name": "ada"})
        self.assertEqual(events[1]["event"], "DELETE")

    def test_list_leaf_value_preserved(self):
        subscription = "subscription { users(id: 1) { id tags } }"
        events_text = (
            '{"op": "INSERT", "entity": "users", "before": null,'
            ' "after": {"id": 1, "tags": ["a", "b"]}}\n'
        )
        events = self.assert_events(
            *self.run_cli(subscription=subscription, events=events_text)
        )
        self.assertEqual(events[0]["data"]["tags"], ["a", "b"])

    def test_operation_selected_by_name(self):
        subscription = (
            "query Q { users { id } }\n"
            "subscription S { users(id: 1) { id } }\n"
        )
        events = self.assert_events(
            *self.run_cli(subscription=subscription, operation="S")
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["subscription"], "S")

    def test_variable_default_used_when_not_provided(self):
        subscription = "subscription ($id: ID = 2) { users(id: $id) { id } }"
        events = self.assert_events(
            *self.run_cli(subscription=subscription, variables="{}")
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "DELETE")

    def test_no_matching_events_outputs_nothing(self):
        code, stdout, stderr = self.run_cli(variables='{"id": 404}')
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_blank_lines_and_missing_fields(self):
        events_text = (
            '\n'
            '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1}}\n'
        )
        events = self.assert_events(*self.run_cli(events=events_text))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["name"], None)

    # -- operation selection errors -------------------------------------------

    def test_multiple_operations_require_name(self):
        subscription = "subscription A { users { id } } subscription B { users { id } }"
        self.assert_error(*self.run_cli(subscription=subscription), "InvalidQuery")

    def test_operation_name_must_be_subscription(self):
        subscription = "query A { users { id } }"
        self.assert_error(
            *self.run_cli(subscription=subscription, operation="A"), "InvalidQuery"
        )

    def test_unknown_operation_name(self):
        self.assert_error(*self.run_cli(operation="Nope"), "InvalidQuery")

    def test_single_query_operation_rejected(self):
        self.assert_error(
            *self.run_cli(subscription="query { users { id } }"), "InvalidQuery"
        )

    def test_no_operations(self):
        self.assert_error(
            *self.run_cli(subscription="fragment F on User { id }"), "InvalidQuery"
        )

    def test_multiple_root_fields_rejected(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users { id } teams { id } }"),
            "InvalidQuery",
        )

    # -- selection errors ------------------------------------------------------

    def test_empty_root_selection(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users { } }"), "InvalidQuery"
        )

    def test_unknown_leaf_field(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users { nope } }"),
            "InvalidQuery",
        )

    def test_leaf_with_selection(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users { id { x } } }"),
            "InvalidQuery",
        )

    def test_nested_object_unsupported(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users { team { id } } }"),
            "UnsupportedSelection",
        )

    def test_non_filter_argument(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users(team: 1) { id } }"),
            "InvalidQuery",
        )

    def test_unknown_root_field(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { nope { id } }"), "InvalidQuery"
        )

    def test_root_field_not_entity(self):
        schema = SCHEMA + "extend type Subscription { version: Version }\n" \
                          "type Version { tag: String }\n"
        self.assert_error(
            *self.run_cli(subscription="subscription { version { tag } }",
                          schema=schema),
            "MappingError",
        )

    def test_missing_subscription_root_type(self):
        schema = "type Query { users: [User!]! }\n" \
                 'type User @entity(name: "users", key: "id") { id: ID! }\n'
        self.assert_error(*self.run_cli(schema=schema), "MappingError")

    # -- variables errors --------------------------------------------------------

    def test_variable_missing(self):
        self.assert_error(*self.run_cli(variables="{}"), "VariablesError")

    def test_variable_wrong_type(self):
        self.assert_error(*self.run_cli(variables='{"id": {}}'), "VariablesError")

    def test_variable_undeclared(self):
        subscription = "subscription { users(id: $id) { id } }"
        self.assert_error(*self.run_cli(subscription=subscription), "VariablesError")

    def test_variables_file_not_an_object(self):
        self.assert_error(*self.run_cli(variables="[1]"), "VariablesError")

    # -- event errors --------------------------------------------------------------

    def test_event_invalid_json(self):
        self.assert_error(*self.run_cli(events="{not json\n"), "EventError")

    def test_event_not_an_object(self):
        self.assert_error(*self.run_cli(events="[1, 2]\n"), "EventError")

    def test_event_missing_key(self):
        events = '{"op": "INSERT", "entity": "users", "after": {}}\n'
        self.assert_error(*self.run_cli(events=events), "EventError")

    def test_event_unknown_op(self):
        events = '{"op": "UPSERT", "entity": "users", "before": null, "after": {}}\n'
        self.assert_error(*self.run_cli(events=events), "EventError")

    def test_event_snapshot_not_object(self):
        events = '{"op": "INSERT", "entity": "users", "before": null, "after": 5}\n'
        self.assert_error(*self.run_cli(events=events), "EventError")

    def test_event_missing_snapshot(self):
        events = '{"op": "INSERT", "entity": "users", "before": null, "after": null}\n'
        self.assert_error(*self.run_cli(events=events), "EventError")

    # -- parse / io errors ------------------------------------------------------------

    def test_subscription_parse_error(self):
        self.assert_error(
            *self.run_cli(subscription="subscription { users { id "), "ParseError"
        )

    def test_variables_parse_error(self):
        self.assert_error(*self.run_cli(variables="{nope"), "ParseError")

    def test_missing_events_file(self):
        schema_path = self._write("schema.graphql", SCHEMA)
        subscription_path = self._write("subscription.graphql", SUBSCRIPTION)
        variables_path = self._write("variables.json", "{}")
        argv = ["subscription-push", "--schema", schema_path,
                "--subscription", subscription_path,
                "--variables", variables_path,
                "--events", "/nonexistent/events.ndjson"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")

    def test_non_utf8_events_file(self):
        events_path = self._write("events.ndjson", b"\xff\xfe{}", mode="wb")
        schema_path = self._write("schema.graphql", SCHEMA)
        subscription_path = self._write("subscription.graphql", SUBSCRIPTION)
        variables_path = self._write("variables.json", "{}")
        argv = ["subscription-push", "--schema", schema_path,
                "--subscription", subscription_path,
                "--variables", variables_path,
                "--events", events_path]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")


if __name__ == "__main__":
    unittest.main()
