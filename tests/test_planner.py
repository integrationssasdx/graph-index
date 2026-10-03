"""End-to-end and unit tests for the query planner.

Run with::

    python3 -m unittest discover -s tests

The CLI cases exercise the real ``graph-index`` entry point via the
package's ``main`` so the stdout/stderr/exit-code contract is covered.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graphindex import cli
from graphindex.errors import PlannerError
from graphindex.mapping import build_mapping
from graphindex.parser import parse
from graphindex.planner import build_plan

SCHEMA = """
type Query {
  user(id: ID): User
  users(status: Status): [User!]!
  team(id: ID): Team
}
type Mutation {
  createUser(name: String!, team_id: ID): User
}
enum Status { ACTIVE INACTIVE }

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: Status
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
}
type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
  lead_id: ID
  lead: User @link(local: "lead_id", target: "id")
}
"""


def plan(query, variables=None, schema=SCHEMA, name=None):
    return build_plan(build_mapping(parse(schema)), parse(query),
                      variables or {}, name)


class PlannerTestCase(unittest.TestCase):
    @contextlib.contextmanager
    def assertRaisesPlanner(self, code):
        try:
            yield
        except PlannerError as error:
            self.assertEqual(error.code, code,
                             f"expected {code}, got {error.code}: {error}")
        else:
            self.fail(f"expected PlannerError with code {code}")


class PlanShapeTests(PlannerTestCase):
    def test_basic_scan_filter_and_join(self):
        q = """
        query GetUser($id: ID!) {
          user(id: $id) {
            id
            display: name
            team { id name }
          }
        }
        """
        result = plan(q, {"id": "u-1"})
        self.assertEqual(result["operationType"], "query")
        self.assertEqual(result["operationName"], "GetUser")
        self.assertEqual(len(result["roots"]), 1)
        root = result["roots"][0]
        self.assertEqual(root["path"], ["user"])
        self.assertEqual(root["entity"], "users")
        self.assertEqual(root["filter"], {"id": "u-1"})
        self.assertEqual(root["fields"],
                         [["user", "id"], ["user", "display"]])
        self.assertEqual(len(result["joins"]), 1)
        join = result["joins"][0]
        self.assertEqual(join["path"], ["user", "team"])
        self.assertEqual(join["fromEntity"], "users")
        self.assertEqual(join["fromField"], "team_id")
        self.assertEqual(join["toEntity"], "teams")
        self.assertEqual(join["toField"], "id")
        self.assertEqual(join["fields"],
                         [["user", "team", "id"], ["user", "team", "name"]])

    def test_literal_argument_preserved(self):
        result = plan("{ user(id: 42) { id } }")
        self.assertEqual(result["operationName"], None)
        self.assertEqual(result["roots"][0]["filter"],
                         {"id": 42})

    def test_id_variable_accepts_integer(self):
        result = plan("query Q($id: ID!) { user(id: $id) { id } }",
                      {"id": 7})
        self.assertEqual(result["roots"][0]["filter"]["id"], "7")

    def test_fragment_expansion_dedup_and_order(self):
        q = """
        query Q {
          user(id: "1") {
            id
            ...A
            ...B
          }
        }
        fragment A on User { name team { id name } }
        fragment B on User { name status team { name } }
        """
        result = plan(q)
        root = result["roots"][0]
        self.assertEqual(root["fields"],
                         [["user", "id"], ["user", "name"],
                          ["user", "status"]])
        join = result["joins"][0]
        self.assertEqual(len(result["joins"]), 1)
        self.assertEqual(join["fields"],
                         [["user", "team", "id"], ["user", "team", "name"]])

    def test_multiple_roots_and_chained_join(self):
        q = """
        query Q {
          user(id: "1") { id team { id lead { id name } } }
          team(id: "9") { id }
        }
        """
        result = plan(q)
        self.assertEqual([r["path"] for r in result["roots"]],
                         [["user"], ["team"]])
        self.assertEqual([j["path"] for j in result["joins"]],
                         [["user", "team"], ["user", "team", "lead"]])

    def test_mutation(self):
        result = plan(
            'mutation M($n: String!) { createUser(name: $n) { id } }',
            {"n": "bob"})
        self.assertEqual(result["operationType"], "mutation")
        self.assertEqual(result["roots"][0]["filter"],
                         {"name": "bob"})


class OperationSelectionTests(PlannerTestCase):
    def test_single_named_auto_selected(self):
        q = "query Only { user(id: \"1\") { id } }"
        self.assertEqual(plan(q)["operationName"], "Only")

    def test_single_mutation_auto_selected(self):
        q = 'mutation Only { createUser(name: "x") { id } }'
        self.assertEqual(plan(q)["operationType"], "mutation")

    def test_multiple_requires_name(self):
        q = """
        query A { user(id: "1") { id } }
        query B { team(id: "2") { id } }
        """
        with self.assertRaisesPlanner("InvalidRequest"):
            plan(q)

    def test_explicit_name(self):
        q = """
        query A { user(id: "1") { id } }
        query B { team(id: "2") { id } }
        """
        self.assertEqual(plan(q, name="B")["roots"][0]["entity"], "teams")

    def test_invalid_name(self):
        q = 'query A { user(id: "1") { id } }'
        with self.assertRaisesPlanner("InvalidOperation"):
            plan(q, name="Nope")

    def test_subscription_unsupported(self):
        schema = "type Subscription { user(id: ID): User }\n" + SCHEMA
        q = 'subscription S { user(id: "1") { id } }'
        with self.assertRaisesPlanner("UnsupportedOperation"):
            plan(q, schema=schema)


class VariableTests(PlannerTestCase):
    def test_missing_required(self):
        with self.assertRaisesPlanner("VariablesError"):
            plan("query Q($id: ID!) { user(id: $id) { id } }", {})

    def test_null_for_nonnull(self):
        with self.assertRaisesPlanner("VariablesError"):
            plan("query Q($id: ID!) { user(id: $id) { id } }",
                 {"id": None})

    def test_wrong_type(self):
        with self.assertRaisesPlanner("VariablesError"):
            plan("query Q($s: String!) { user(id: $s) { id } }",
                 {"s": 123})

    def test_int_type_rejects_string(self):
        schema = SCHEMA.replace(
            "  user(id: ID): User",
            "  user(id: ID, count: Int): User").replace(
            "  status: Status\n",
            "  status: Status\n  count: Int\n")
        with self.assertRaisesPlanner("VariablesError"):
            plan("query Q($n: Int!) { user(count: $n) { id } }",
                 {"n": "7"}, schema=schema)

    def test_boolean_rejects_string(self):
        schema = SCHEMA.replace(
            "  user(id: ID): User",
            "  user(id: ID, active: Boolean): User").replace(
            "  status: Status\n",
            "  status: Status\n  active: Boolean\n")
        with self.assertRaisesPlanner("VariablesError"):
            plan("query Q($b: Boolean!) { user(active: $b) { id } }",
                 {"b": "true"}, schema=schema)

    def test_enum_value_checked(self):
        with self.assertRaisesPlanner("VariablesError"):
            plan("query Q($s: Status!) { users(status: $s) { id } }",
                 {"s": "BANISHED"})

    def test_enum_value_ok(self):
        result = plan(
            "query Q($s: Status!) { users(status: $s) { id } }",
            {"s": "ACTIVE"})
        self.assertEqual(result["roots"][0]["filter"],
                         {"status": "ACTIVE"})

    def test_nullable_missing_resolves_to_null_filter(self):
        result = plan("query Q($id: ID) { user(id: $id) { id } }", {})
        self.assertEqual(result["roots"][0]["filter"],
                         {"id": None})




class FieldMappingErrorTests(PlannerTestCase):
    def test_unknown_entity_field(self):
        with self.assertRaisesPlanner("UnknownField"):
            plan('{ user(id: "1") { id nonexistent } }')

    def test_unknown_root_field(self):
        with self.assertRaisesPlanner("UnknownField"):
            plan('{ nonexistent { id } }')

    def test_unknown_argument(self):
        with self.assertRaisesPlanner("UnknownField"):
            plan('{ user(id: "1", bogus: 2) { id } }')

    def test_scalar_argument_rejected(self):
        with self.assertRaisesPlanner("UnknownField"):
            plan('{ user(id: "1") { name(x: 1) } }')

    def test_link_to_unmapped_type(self):
        schema = """
        type Query { thing(id: ID): Thing }
        type Thing @entity(name: "things", key: "id") {
          id: ID!
          owner: Owner @link(local: "owner_id", target: "id")
        }
        """
        with self.assertRaisesPlanner("UnknownEntity"):
            build_mapping(parse(schema))

    def test_invalid_join_target_not_key(self):
        schema = """
        type Query { user(id: ID): User }
        type User @entity(name: "users", key: "id") {
          id: ID!
          team_name: String
          team: Team @link(local: "team_name", target: "name")
        }
        type Team @entity(name: "teams", key: "id") {
          id: ID! name: String!
        }
        """
        with self.assertRaisesPlanner("InvalidJoin"):
            build_mapping(parse(schema))

    def test_missing_entity_directive_arguments(self):
        schema = 'type User @entity(name: "users") { id: ID! }\n' \
                 'type Query { user(id: ID): User }'
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_missing_entity_directive(self):
        # A root field returning a type without @entity is a mapping error.
        schema = "type Query { user(id: ID): User }\n" \
                 "type User { id: ID! }"
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_key_not_a_field(self):
        schema = """
        type Query { user(id: ID): User }
        type User @entity(name: "users", key: "missing") { id: ID! }
        """
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_duplicate_table_mapping(self):
        schema = """
        type Query { a(id: ID): A }
        type A @entity(name: "shared", key: "id") { id: ID! }
        type B @entity(name: "shared", key: "id") { id: ID! }
        """
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_link_missing_arg(self):
        schema = """
        type Query { user(id: ID): User }
        type User @entity(name: "users", key: "id") {
          id: ID!
          team: Team @link(local: "team_id")
        }
        type Team @entity(name: "teams", key: "id") { id: ID! }
        """
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_object_field_without_link_is_mapping_error(self):
        schema = """
        type Query { user(id: ID): User }
        type User @entity(name: "users", key: "id") {
          id: ID!
          team: Team
        }
        type Team @entity(name: "teams", key: "id") { id: ID! }
        """
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_plain_object_type_field_is_mapping_error(self):
        schema = """
        type Query { user(id: ID): User }
        type User @entity(name: "users", key: "id") {
          id: ID!
          avatar: Picture
        }
        type Picture { url: String }
        """
        with self.assertRaisesPlanner("MappingError"):
            build_mapping(parse(schema))

    def test_extend_type_is_merged(self):
        schema = """
        type Query { user(id: ID): User }
        type User @entity(name: "users", key: "id") { id: ID! }
        extend type User {
          name: String
          team_id: ID
          team: Team @link(local: "team_id", target: "id")
        }
        type Team @entity(name: "teams", key: "id") { id: ID! name: String }
        """
        result = plan('{ user(id: "1") { id name team { name } } }',
                      schema=schema)
        self.assertEqual(result["roots"][0]["fields"],
                         [["user", "id"], ["user", "name"]])
        self.assertEqual(result["joins"][0]["fromField"], "team_id")



class QueryValidityTests(PlannerTestCase):
    def test_empty_operation(self):
        with self.assertRaisesPlanner("InvalidQuery"):
            plan("{ }")

    def test_empty_entity_selection(self):
        with self.assertRaisesPlanner("InvalidQuery"):
            plan('{ user(id: "1") { } }')

    def test_linked_field_without_selection(self):
        with self.assertRaisesPlanner("InvalidQuery"):
            plan('{ user(id: "1") { id team } }')

    def test_scalar_with_selection(self):
        with self.assertRaisesPlanner("InvalidQuery"):
            plan('{ user(id: "1") { id { x } } }')

    def test_unknown_fragment(self):
        with self.assertRaisesPlanner("UnknownField"):
            plan('{ user(id: "1") { ...Nope } }')

    def test_fragment_cycle(self):
        q = """
        { user(id: "1") { ...A } }
        fragment A on User { ...B id }
        fragment B on User { ...A }
        """
        with self.assertRaisesPlanner("InvalidQuery"):
            plan(q)

    def test_path_scalar_object_conflict(self):
        with self.assertRaisesPlanner("InvalidQuery"):
            plan('{ user(id: "1") { team team { id } } }')

    def test_alias_field_conflict(self):
        q = """
        { user(id: "1") {
            x: id
            x: name
          } }
        """
        with self.assertRaisesPlanner("InvalidQuery"):
            plan(q)



class ParseErrorTests(PlannerTestCase):
    def test_bad_graphql(self):
        with self.assertRaisesPlanner("ParseError"):
            parse("type User { id: ID")

    def test_bad_number(self):
        with self.assertRaisesPlanner("ParseError"):
            parse("{ user(id: 12.) { id } }")

    def test_unterminated_string(self):
        with self.assertRaisesPlanner("ParseError"):
            parse('{ user(id: "x) { id } }')



class CliTests(PlannerTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.schema_path = os.path.join(self.tmp, "schema.graphql")
        self.query_path = os.path.join(self.tmp, "query.graphql")
        self.vars_path = os.path.join(self.tmp, "variables.json")
        with open(self.schema_path, "w", encoding="utf-8") as f:
            f.write(SCHEMA)

    def _invoke(self, schema_path=None, query_path=None, variables=None,
                name=None):
        if query_path is not None:
            with open(self.query_path, "w", encoding="utf-8") as f:
                f.write(query_path)
        argv = ["query-plan", "--schema", schema_path or self.schema_path,
                "--query", self.query_path]
        if variables is not None:
            with open(self.vars_path, "w", encoding="utf-8") as f:
                if isinstance(variables, str):
                    f.write(variables)
                else:
                    json.dump(variables, f)
            argv += ["--variables", self.vars_path]
        if name is not None:
            argv += ["--operation-name", name]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as real_out, \
                contextlib.redirect_stderr(io.StringIO()) as real_err:
            rc = cli.main(argv)
            return rc, real_out.getvalue(), real_err.getvalue()

    def test_cli_success(self):
        rc, out, err = self._invoke(
            query_path='{ user(id: "1") { id team { name } } }')
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["roots"][0]["entity"], "users")
        self.assertEqual(err, "")

    def test_cli_error_json_on_stderr(self):
        rc, out, err = self._invoke(query_path="{ ")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        payload = json.loads(err)
        self.assertEqual(payload["code"], "ParseError")
        self.assertIn("message", payload)

    def test_cli_missing_schema_file(self):
        with open(self.query_path, "w") as f:
            f.write('{ user(id: "1") { id } }')
        argv = ["query-plan",
                "--schema", os.path.join(self.tmp, "missing-schema.graphql"),
                "--query", self.query_path]
        with contextlib.redirect_stdout(io.StringIO()) as real_out, \
                contextlib.redirect_stderr(io.StringIO()) as real_err:
            rc = cli.main(argv)
        self.assertEqual(rc, 2)
        self.assertEqual(real_out.getvalue(), "")
        self.assertEqual(json.loads(real_err.getvalue())["code"], "IoError")

    def test_cli_non_utf8(self):
        raw_path = os.path.join(self.tmp, "raw.graphql")
        with open(raw_path, "wb") as f:
            f.write(b"\xff\xfe{ user")
        with open(self.query_path, "w") as f:
            f.write('{ user(id: "1") { id } }')
        rc, out, err = self._run_raw(raw_path)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["code"], "IoError")

    def _run_raw(self, raw_path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as real_out, \
                contextlib.redirect_stderr(io.StringIO()) as real_err:
            rc = cli.main(["query-plan", "--schema", raw_path,
                           "--query", self.query_path])
        return rc, real_out.getvalue(), real_err.getvalue()

    def test_cli_bad_json_variables(self):
        rc, out, err = self._invoke(
            query_path='{ user(id: "1") { id } }',
            variables="[1, 2]")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["code"], "ParseError")

    def test_cli_missing_query_file(self):
        argv = ["query-plan", "--schema", self.schema_path,
                "--query", os.path.join(self.tmp, "nope.graphql")]
        with contextlib.redirect_stdout(io.StringIO()) as real_out, \
                contextlib.redirect_stderr(io.StringIO()) as real_err:
            rc = cli.main(argv)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(real_err.getvalue())["code"], "IoError")


if __name__ == "__main__":
    unittest.main()
