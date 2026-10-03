"""End-to-end tests for `graph-index query-plan`."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from graph_index.cli import main

SCHEMA = """\
type Query {
  users(id: ID, name: String): [User!]!
  user(id: ID!): User
  teams: [Team!]!
}

type Mutation {
  createUser(name: String!): User
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
  members: [User!]! @link(local: "id", target: "team_id")
}
"""

COMPOSITE_SCHEMA = """\
type Query {
  transfers(chain_id: ID, id: ID): [Transfer!]!
  tokens: [Token!]!
}

type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
  chain_id: ID!
  id: ID!
  amount: Int!
  token_chain: ID
  token_addr: ID
  token: Token @link(local: ["token_chain", "token_addr"], target: ["chain_id", "address"])
}

type Token @entity(name: "tokens", key: ["chain_id", "address"]) {
  chain_id: ID!
  address: ID!
  symbol: String!
}
"""


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.schema_path = self._write("schema.graphql", SCHEMA)
        self.variables_path = self._write("variables.json", "{}")

    def _write(self, name, content, mode="w"):
        path = os.path.join(self.tmp.name, name)
        with open(path, mode) as handle:
            handle.write(content)
        return path

    def run_cli(self, query, variables="{}", schema=SCHEMA, operation=None,
                query_name="query.graphql"):
        query_path = self._write(query_name, query)
        variables_path = self._write("variables.json", variables)
        schema_path = self._write("schema.graphql", schema)
        argv = ["query-plan", "--schema", schema_path, "--query", query_path,
                "--variables", variables_path]
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

    def assert_plan(self, code, stdout, stderr):
        self.assertEqual(stderr, "")
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    # -- happy paths ---------------------------------------------------------

    def test_basic_query_with_variable_and_join(self):
        query = """
        query GetUsers($id: ID) {
          users(id: $id) {
            id
            name
            team { id name }
          }
        }
        """
        plan = self.assert_plan(*self.run_cli(query, '{"id": 7}'))
        self.assertEqual(plan["operationType"], "query")
        self.assertEqual(plan["operationName"], "GetUsers")
        self.assertEqual(len(plan["roots"]), 1)
        root = plan["roots"][0]
        self.assertEqual(root["path"], "users")
        self.assertEqual(root["entity"], "users")
        self.assertEqual(root["filter"], {"id": 7})
        self.assertEqual(
            root["fields"],
            ["users.id", "users.name", "users.team.id", "users.team.name"],
        )
        self.assertEqual(
            plan["joins"],
            [{"path": "users.team", "fromEntity": "users", "fromField": "team_id",
              "toEntity": "teams", "toField": "id"}],
        )

    def test_anonymous_operation_and_literal_filter(self):
        plan = self.assert_plan(*self.run_cli('{ users(name: "ada") { id } }'))
        self.assertEqual(plan["operationType"], "query")
        self.assertIsNone(plan["operationName"])
        self.assertEqual(plan["roots"][0]["filter"], {"name": "ada"})
        self.assertEqual(plan["joins"], [])

    def test_mutation(self):
        plan = self.assert_plan(*self.run_cli('mutation { createUser(name: "ada") { id } }'))
        self.assertEqual(plan["operationType"], "mutation")
        self.assertEqual(plan["roots"][0]["entity"], "users")
        self.assertEqual(plan["roots"][0]["filter"], {"name": "ada"})

    def test_aliases_and_multiple_roots(self):
        query = "{ me: user(id: 1) { userId: id } teams { name } }"
        plan = self.assert_plan(*self.run_cli(query))
        self.assertEqual([r["path"] for r in plan["roots"]], ["me", "teams"])
        self.assertEqual(plan["roots"][0]["filter"], {"id": 1})
        self.assertEqual(plan["roots"][0]["fields"], ["me.userId"])
        self.assertEqual(plan["roots"][1]["entity"], "teams")

    def test_fragments_expanded_and_deduplicated(self):
        query = """
        query {
          users { id ...Bits team { id ...TeamBits } }
        }
        fragment Bits on User { id name }
        fragment TeamBits on Team { id name }
        """
        plan = self.assert_plan(*self.run_cli(query))
        root = plan["roots"][0]
        self.assertEqual(
            root["fields"],
            ["users.id", "users.name", "users.team.id", "users.team.name"],
        )
        self.assertEqual(len(plan["joins"]), 1)

    def test_variable_default_used_when_not_provided(self):
        query = "query ($name: String = \"ada\") { users(name: $name) { id } }"
        plan = self.assert_plan(*self.run_cli(query))
        self.assertEqual(plan["roots"][0]["filter"], {"name": "ada"})

    def test_operation_selected_by_name(self):
        query = """
        query A { users { id } }
        query B { teams { name } }
        """
        plan = self.assert_plan(*self.run_cli(query, operation="B"))
        self.assertEqual(plan["operationName"], "B")
        self.assertEqual(plan["roots"][0]["entity"], "teams")

    def test_deep_join_chain(self):
        schema = SCHEMA + """
        extend type Team { league: League @link(local: "league_id", target: "id") }
        extend type Team { league_id: ID }
        type League @entity(name: "leagues", key: "id") { id: ID! title: String }
        """
        query = "{ teams { name league { title } } }"
        plan = self.assert_plan(*self.run_cli(query, schema=schema))
        self.assertEqual(
            plan["joins"],
            [{"path": "teams.league", "fromEntity": "teams", "fromField": "league_id",
              "toEntity": "leagues", "toField": "id"}],
        )
        self.assertEqual(plan["roots"][0]["fields"], ["teams.name", "teams.league.title"])

    # -- composite keys / links ----------------------------------------------

    def test_composite_key_join_emits_field_arrays(self):
        query = "{ transfers { id amount token { symbol } } }"
        plan = self.assert_plan(*self.run_cli(query, schema=COMPOSITE_SCHEMA))
        root = plan["roots"][0]
        self.assertEqual(root["entity"], "transfers")
        self.assertEqual(root["filter"], {})
        self.assertEqual(
            root["fields"],
            ["transfers.id", "transfers.amount", "transfers.token.symbol"],
        )
        self.assertEqual(
            plan["joins"],
            [{
                "path": "transfers.token",
                "fromEntity": "transfers",
                "fromField": ["token_chain", "token_addr"],
                "toEntity": "tokens",
                "toField": ["chain_id", "address"],
            }],
        )

    def test_composite_join_is_a_single_join(self):
        # A two-field composite link must produce one join, not two.
        query = "{ transfers { token { symbol } } }"
        plan = self.assert_plan(*self.run_cli(query, schema=COMPOSITE_SCHEMA))
        self.assertEqual(len(plan["joins"]), 1)
        self.assertEqual(plan["joins"][0]["fromField"], ["token_chain", "token_addr"])

    def test_composite_filter_on_scalar_root_args(self):
        query = "{ transfers(chain_id: 1, id: 7) { id } }"
        plan = self.assert_plan(*self.run_cli(query, schema=COMPOSITE_SCHEMA))
        self.assertEqual(plan["roots"][0]["filter"], {"chain_id": 1, "id": 7})

    def test_composite_link_alias_and_leaf_dedup(self):
        query = "{ transfers { t: token { symbol } token { symbol } } }"
        plan = self.assert_plan(*self.run_cli(query, schema=COMPOSITE_SCHEMA))
        # alias keeps its own path; un-aliased duplicate merges via expansion,
        # leaving one join per response path.
        paths = sorted(j["path"] for j in plan["joins"])
        self.assertEqual(paths, ["transfers.t", "transfers.token"])
        for join in plan["joins"]:
            self.assertEqual(join["fromField"], ["token_chain", "token_addr"])
            self.assertEqual(join["toField"], ["chain_id", "address"])

    def test_single_element_list_link_emits_single_join_with_arrays(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: "id") {
          id: ID!
          tid: ID
          token: Token @link(local: ["tid"], target: ["id"])
        }
        type Token @entity(name: "tokens", key: "id") { id: ID! symbol: String }
        """
        query = "{ transfers { id token { symbol } } }"
        plan = self.assert_plan(*self.run_cli(query, schema=schema))
        self.assertEqual(len(plan["joins"]), 1)
        join = plan["joins"][0]
        self.assertEqual(join["fromField"], ["tid"])
        self.assertEqual(join["toField"], ["id"])

    def test_scalar_key_and_link_outputs_unchanged_under_composite(self):
        # Existing single-field plans keep emitting string field names.
        query = "{ users { id team { id name } } }"
        plan = self.assert_plan(*self.run_cli(query))
        join = plan["joins"][0]
        self.assertIsInstance(join["fromField"], str)
        self.assertIsInstance(join["toField"], str)
        self.assertEqual(join["fromField"], "team_id")
        self.assertEqual(join["toField"], "id")

    # -- composite mapping / join errors -------------------------------------

    def test_composite_target_wrong_order_is_invalid_join(self):
        schema = COMPOSITE_SCHEMA.replace(
            'target: ["chain_id", "address"]',
            'target: ["address", "chain_id"]',
        )
        self.assert_error(
            *self.run_cli("{ transfers { token { symbol } } }", schema=schema),
            "InvalidJoin",
        )

    def test_composite_target_wrong_set_is_invalid_join(self):
        # Both target fields exist on Token (symbol is a scalar field), so the
        # declaration is legal, but they do not fully name the composite key.
        schema = COMPOSITE_SCHEMA.replace(
            'target: ["chain_id", "address"]',
            'target: ["chain_id", "symbol"]',
        )
        self.assert_error(
            *self.run_cli("{ transfers { token { symbol } } }", schema=schema),
            "InvalidJoin",
        )

    def test_empty_composite_key_list(self):
        schema = (
            "type Query { transfers: [Transfer!]! }\n"
            'type Transfer @entity(name: "transfers", key: []) { id: ID! }\n'
        )
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_duplicate_composite_key_elements(self):
        schema = (
            "type Query { transfers: [Transfer!]! }\n"
            'type Transfer @entity(name: "transfers", key: ["id", "id"]) { id: ID! }\n'
        )
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_key_missing_field(self):
        schema = (
            "type Query { transfers: [Transfer!]! }\n"
            'type Transfer @entity(name: "transfers", key: ["id", "nope"])'
            " { id: ID! }\n"
        )
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_key_non_scalar_element(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: ["id", "extra"]) {
          id: ID!
          extra: Token
        }
        type Token { x: ID }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_key_non_string_element(self):
        schema = (
            "type Query { transfers: [Transfer!]! }\n"
            'type Transfer @entity(name: "transfers", key: ["id", 3]) { id: ID! }\n'
        )
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_key_wrong_param_form(self):
        schema = (
            "type Query { transfers: [Transfer!]! }\n"
            'type Transfer @entity(name: "transfers", key: 3) { id: ID! }\n'
        )
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_local_target_length_mismatch(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
          a: ID
          token: Token @link(local: ["a"], target: ["chain_id", "address"])
        }
        type Token @entity(name: "tokens", key: ["chain_id", "address"]) {
          chain_id: ID!
          address: ID!
        }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_mixed_forms(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: "id") {
          id: ID!
          a: ID
          token: Token @link(local: "a", target: ["id"])
        }
        type Token @entity(name: "tokens", key: "id") { id: ID! }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_duplicate_local_elements(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
          a: ID
          token: Token @link(local: ["a", "a"], target: ["chain_id", "id"])
        }
        type Token @entity(name: "tokens", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
        }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_empty_local_list(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: "id") {
          id: ID!
          token: Token @link(local: [], target: [])
        }
        type Token @entity(name: "tokens", key: "id") { id: ID! }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_local_missing_field(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
          b: ID
          token: Token @link(local: ["nope", "b"], target: ["chain_id", "id"])
        }
        type Token @entity(name: "tokens", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
        }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_local_non_scalar(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
          b: ID
          a: Token
          token: Token @link(local: ["a", "b"], target: ["chain_id", "id"])
        }
        type Token @entity(name: "tokens", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
        }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_target_missing_field(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
          a: ID
          b: ID
          token: Token @link(local: ["a", "b"], target: ["chain_id", "ghost"])
        }
        type Token @entity(name: "tokens", key: ["chain_id", "id"]) {
          chain_id: ID!
          id: ID!
        }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    def test_composite_link_wrong_param_form(self):
        schema = """
        type Query { transfers: [Transfer!]! }
        type Transfer @entity(name: "transfers", key: "id") {
          id: ID!
          token: Token @link(local: 1, target: "id")
        }
        type Token @entity(name: "tokens", key: "id") { id: ID! }
        """
        self.assert_error(
            *self.run_cli("{ transfers { id } }", schema=schema), "MappingError"
        )

    # -- operation selection errors -------------------------------------------

    def test_multiple_operations_require_name(self):
        query = "query A { users { id } } query B { teams { id } }"
        self.assert_error(*self.run_cli(query), "InvalidRequest")

    def test_invalid_operation_name(self):
        self.assert_error(
            *self.run_cli("query A { users { id } }", operation="Nope"),
            "InvalidOperation",
        )

    def test_subscription_unsupported(self):
        self.assert_error(
            *self.run_cli("subscription { users { id } }"), "UnsupportedOperation"
        )

    def test_subscription_selected_by_name(self):
        query = "query A { users { id } } subscription S { users { id } }"
        self.assert_error(*self.run_cli(query, operation="S"), "UnsupportedOperation")

    # -- field / entity errors ---------------------------------------------------

    def test_unknown_root_field(self):
        self.assert_error(*self.run_cli("{ nope { id } }"), "UnknownField")

    def test_unknown_nested_field(self):
        self.assert_error(*self.run_cli("{ users { nope } }"), "UnknownField")

    def test_unknown_argument(self):
        self.assert_error(*self.run_cli("{ users(nope: 1) { id } }"), "UnknownField")

    def test_argument_not_an_entity_field(self):
        schema = SCHEMA.replace("users(id: ID, name: String)", "users(id: ID, age: Int)")
        self.assert_error(*self.run_cli("{ users(age: 3) { id } }", schema=schema),
                          "UnknownField")

    def test_missing_required_argument(self):
        self.assert_error(*self.run_cli("{ user { id } }"), "UnknownField")

    def test_root_return_type_not_entity(self):
        schema = SCHEMA + "extend type Query { version: Version }\ntype Version { tag: String }\n"
        self.assert_error(*self.run_cli("{ version { tag } }", schema=schema),
                          "UnknownEntity")

    def test_link_target_not_entity(self):
        schema = SCHEMA + """
        extend type User { profile: Profile @link(local: "team_id", target: "id") }
        type Profile { id: ID! }
        """
        self.assert_error(*self.run_cli("{ users { profile { id } } }", schema=schema),
                          "UnknownEntity")

    # -- variables errors ----------------------------------------------------------

    def test_variable_missing(self):
        self.assert_error(
            *self.run_cli("query ($id: ID!) { users(id: $id) { id } }"),
            "VariablesError",
        )

    def test_variable_wrong_type(self):
        self.assert_error(
            *self.run_cli("query ($id: ID) { users(id: $id) { id } }", '{"id": {}}'),
            "VariablesError",
        )

    def test_variable_undeclared(self):
        self.assert_error(
            *self.run_cli("{ users(id: $id) { id } }", '{"id": 1}'),
            "VariablesError",
        )

    def test_variables_file_not_an_object(self):
        self.assert_error(*self.run_cli("{ users { id } }", "[1, 2]"), "VariablesError")

    # -- mapping / join errors -------------------------------------------------------

    def test_entity_missing_key(self):
        schema = "type Query { users: [User!]! }\ntype User @entity(name: \"users\") { id: ID! }\n"
        self.assert_error(*self.run_cli("{ users { id } }", schema=schema), "MappingError")

    def test_entity_key_not_a_field(self):
        schema = "type Query { users: [User!]! }\ntype User @entity(name: \"users\", key: \"uid\") { id: ID! }\n"
        self.assert_error(*self.run_cli("{ users { id } }", schema=schema), "MappingError")

    def test_duplicate_entity_table(self):
        schema = """
        type Query { users: [User!]! }
        type User @entity(name: "users", key: "id") { id: ID! }
        type Account @entity(name: "users", key: "id") { id: ID! }
        """
        self.assert_error(*self.run_cli("{ users { id } }", schema=schema), "MappingError")

    def test_link_local_field_missing(self):
        schema = """
        type Query { users: [User!]! }
        type User @entity(name: "users", key: "id") {
          id: ID!
          team: Team @link(local: "nope", target: "id")
        }
        type Team @entity(name: "teams", key: "id") { id: ID! }
        """
        self.assert_error(*self.run_cli("{ users { id } }", schema=schema), "MappingError")

    def test_object_field_without_link(self):
        schema = """
        type Query { users: [User!]! }
        type User @entity(name: "users", key: "id") { id: ID! team: Team }
        type Team @entity(name: "teams", key: "id") { id: ID! }
        """
        self.assert_error(*self.run_cli("{ users { team { id } } }", schema=schema),
                          "MappingError")

    def test_link_target_not_primary_key(self):
        self.assert_error(*self.run_cli("{ teams { members { id } } }"), "InvalidJoin")

    # -- query shape errors ------------------------------------------------------------

    def test_object_field_without_selection(self):
        self.assert_error(*self.run_cli("{ users { team } }"), "InvalidQuery")

    def test_scalar_field_with_selection(self):
        self.assert_error(*self.run_cli("{ users { id { x } } }"), "InvalidQuery")

    def test_empty_selection(self):
        self.assert_error(*self.run_cli("query { users { } }"), "InvalidQuery")

    def test_no_operations(self):
        self.assert_error(*self.run_cli("fragment F on User { id }"), "InvalidRequest")

    def test_unknown_fragment(self):
        self.assert_error(*self.run_cli("{ users { ...Nope } }"), "InvalidQuery")

    # -- parse / io errors --------------------------------------------------------------

    def test_query_parse_error(self):
        self.assert_error(*self.run_cli("{ users { id "), "ParseError")

    def test_schema_parse_error(self):
        self.assert_error(*self.run_cli("{ users { id } }", schema="type Query {"),
                          "ParseError")

    def test_variables_parse_error(self):
        self.assert_error(*self.run_cli("{ users { id } }", "{not json"), "ParseError")

    def test_missing_file(self):
        argv = ["query-plan", "--schema", "/nonexistent/schema.graphql",
                "--query", "/nonexistent/query.graphql",
                "--variables", "/nonexistent/variables.json"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")

    def test_non_utf8_file(self):
        path = self._write("bad.graphql", b"\xff\xfe{}", mode="wb")
        self._write("variables.json", "{}")
        schema_path = self._write("schema.graphql", SCHEMA)
        argv = ["query-plan", "--schema", schema_path, "--query", path,
                "--variables", os.path.join(self.tmp.name, "variables.json")]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assert_error(code, stdout.getvalue(), stderr.getvalue(), "IoError")


if __name__ == "__main__":
    unittest.main()
