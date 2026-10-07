"""End-to-end tests for GraphQL introspection in `query-exec`.

Covers ``__schema``, ``__type(name: String!)`` and ``__typename``: standard
metadata, deterministic wrapper/kind structure, unknown-name ``null``,
mixing introspection with entity roots, the events-file requirement, every
InvalidQuery / VariablesError validation rule and zero complexity cost.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from graph_index.cli import main

SCHEMA = """\
scalar DateTime

schema {
  query: Query
  mutation: Mutation
}

directive @cache(ttl: Int = 60) repeatable on FIELD_DEFINITION | QUERY

type Query {
  users(name: String = "anon", active: Boolean = true, status: String): [User!]!
  user(id: ID!): User
}

type Mutation {
  ping: String
}

enum Role {
  ADMIN
  MEMBER
}

interface Node {
  id: ID!
}

type User implements Node @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  role: Role
  tags: [String!]
  birthday: DateTime
}

input UserFilter {
  take: Int = 5
  q: String
  role: Role = ADMIN
}

union Account = User
"""

EVENT = json.dumps({
    "op": "INSERT", "entity": "users", "before": None,
    "after": {"id": 1, "name": "ada", "role": "ADMIN", "tags": ["a"],
              "birthday": "2024-01-01"},
}) + "\n"

EMPTY_EVENTS = "\n"
BROKEN_EVENTS = "{not json\n"

BUILTIN_DIRECTIVES = ["skip", "include", "deprecated", "specifiedBy"]
META_TYPE_NAMES = [
    "__Schema", "__Type", "__Field", "__InputValue", "__EnumValue",
    "__Directive", "__TypeKind", "__DirectiveLocation",
]


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def run_cli(self, query, variables="{}", schema=SCHEMA, events=EMPTY_EVENTS,
                operation=None, limit=None):
        argv = ["query-exec",
                "--schema", self._write("schema.graphql", schema),
                "--query", self._write("query.graphql", query),
                "--variables", self._write("variables.json", variables),
                "--events", self._write("events.ndjson", events)]
        if operation is not None:
            argv += ["--operation", operation]
        env = {} if limit is None else {
            "GRAPHQL_QUERY_COMPLEXITY_LIMIT": str(limit)}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def data(self, query, **kwargs):
        code, stdout, stderr = self.run_cli(query, **kwargs)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)["data"]

    def assert_error(self, query, expected_code, **kwargs):
        code, stdout, stderr = self.run_cli(query, **kwargs)
        self.assertEqual(code, 2, stdout)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], expected_code)
        self.assertTrue(payload["message"])

    # -- __schema root types ---------------------------------------------------

    def test_schema_root_types_present_and_null(self):
        data = self.data(
            "{ __schema { queryType { name kind } "
            "mutationType { name } subscriptionType { name } } }"
        )
        self.assertEqual(data["__schema"], {
            "queryType": {"name": "Query", "kind": "OBJECT"},
            "mutationType": {"name": "Mutation"},
            "subscriptionType": None,
        })

    def test_schema_query_root_defaults_without_schema_definition(self):
        schema = (
            "type Query { users: [User!]! }\n"
            'type User @entity(name: "users", key: "id") { id: ID! }\n'
        )
        data = self.data("{ __schema { queryType { name } } }", schema=schema)
        self.assertEqual(data["__schema"]["queryType"], {"name": "Query"})

    # -- __schema types ---------------------------------------------------------

    def test_schema_types_lists_every_type_in_deterministic_order(self):
        data = self.data("{ __schema { types { name } } }")
        names = [t["name"] for t in data["__schema"]["types"]]
        # User-defined types are all present.
        for expected in ("Int", "Float", "String", "Boolean", "ID", "DateTime",
                         "Query", "Mutation", "User", "Node", "Account",
                         "Role", "UserFilter", *META_TYPE_NAMES):
            self.assertIn(expected, names)
        # Built-in scalars come first, custom scalars in SDL order, and the
        # introspection types are appended at the end.
        self.assertEqual(names[:5],
                         ["Int", "Float", "String", "Boolean", "ID"])
        self.assertEqual(names[5], "DateTime")
        self.assertEqual(names[-len(META_TYPE_NAMES):], META_TYPE_NAMES)
        # No duplicate type entries.
        self.assertEqual(len(names), len(set(names)))

    # -- __schema directives ----------------------------------------------------

    def test_schema_directives_include_builtins_and_user(self):
        data = self.data(
            "{ __schema { directives { name isRepeatable locations } } }"
        )
        dirs = {d["name"]: d for d in data["__schema"]["directives"]}
        for name in BUILTIN_DIRECTIVES:
            self.assertIn(name, dirs)
        self.assertEqual(dirs["skip"]["isRepeatable"], False)
        self.assertIn("FIELD", dirs["skip"]["locations"])
        cache = dirs["cache"]
        self.assertEqual(cache["isRepeatable"], True)
        self.assertEqual(cache["locations"], ["FIELD_DEFINITION", "QUERY"])

    # -- __type kinds -----------------------------------------------------------

    def test_type_scalar(self):
        data = self.data('{ __type(name: "Int") { kind name } }')
        self.assertEqual(data["__type"], {"kind": "SCALAR", "name": "Int"})

    def test_type_object_fields_args_and_wrappers(self):
        query = """
        { __type(name: "User") {
            kind name
            fields(includeDeprecated: true) {
              name isDeprecated deprecationReason
              args {
                name
                defaultValue
                type { kind name ofType { kind name } }
              }
              type { kind name ofType { kind name ofType { kind name } } }
            }
          }
        }
        """
        data = self.data(query)
        t = data["__type"]
        self.assertEqual(t["kind"], "OBJECT")
        self.assertEqual(t["name"], "User")
        fields = {f["name"]: f for f in t["fields"]}
        for f in t["fields"]:
            self.assertFalse(f["isDeprecated"])
            self.assertIsNone(f["deprecationReason"])
        # id: ID! -> NON_NULL(SCALAR ID)
        self.assertEqual(fields["id"]["type"], {
            "kind": "NON_NULL", "name": None,
            "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
        })
        self.assertEqual(fields["id"]["args"], [])
        # tags: [String!] -> LIST(NON_NULL(SCALAR String))
        self.assertEqual(fields["tags"]["type"], {
            "kind": "LIST", "name": None,
            "ofType": {
                "kind": "NON_NULL", "name": None,
                "ofType": {"kind": "SCALAR", "name": "String"},
            },
        })

    def test_type_root_field_args_defaults(self):
        query = (
            '{ __type(name: "Query") { fields { name args { name defaultValue'
            " type { kind name } } } } }"
        )
        data = self.data(query)
        users = {f["name"]: f for f in data["__type"]["fields"]}["users"]
        args = {a["name"]: a for a in users["args"]}
        self.assertEqual(args["name"]["defaultValue"], '"anon"')
        self.assertEqual(args["active"]["defaultValue"], "true")
        self.assertIsNone(args["status"]["defaultValue"])
        self.assertEqual(args["active"]["type"],
                         {"kind": "SCALAR", "name": "Boolean"})

    def test_type_enum_values(self):
        data = self.data(
            '{ __type(name: "Role") { kind name'
            " enumValues { name isDeprecated deprecationReason } } }"
        )
        self.assertEqual(data["__type"]["kind"], "ENUM")
        self.assertEqual([v["name"] for v in data["__type"]["enumValues"]],
                         ["ADMIN", "MEMBER"])
        for value in data["__type"]["enumValues"]:
            self.assertFalse(value["isDeprecated"])
            self.assertIsNone(value["deprecationReason"])

    def test_type_enum_value_on_other_kinds_is_null(self):
        data = self.data(
            '{ __type(name: "Int") { kind fields { name } enumValues { name }'
            " inputFields { name } possibleTypes { name } ofType { name } } }"
        )
        t = data["__type"]
        self.assertEqual(t["kind"], "SCALAR")
        self.assertIsNone(t["fields"])
        self.assertIsNone(t["enumValues"])
        self.assertIsNone(t["inputFields"])
        self.assertIsNone(t["possibleTypes"])
        self.assertIsNone(t["ofType"])

    def test_type_input_fields(self):
        data = self.data(
            '{ __type(name: "UserFilter") { kind name'
            " inputFields { name type { kind name } defaultValue } } }"
        )
        self.assertEqual(data["__type"]["kind"], "INPUT_OBJECT")
        fields = {f["name"]: f for f in data["__type"]["inputFields"]}
        self.assertEqual(fields["take"]["defaultValue"], "5")
        self.assertEqual(fields["take"]["type"],
                         {"kind": "SCALAR", "name": "Int"})
        self.assertIsNone(fields["q"]["defaultValue"])
        self.assertEqual(fields["role"]["defaultValue"], "ADMIN")

    def test_type_union_possible_types(self):
        data = self.data(
            '{ __type(name: "Account") { kind name'
            " possibleTypes { name } } }"
        )
        self.assertEqual(data["__type"], {
            "kind": "UNION", "name": "Account",
            "possibleTypes": [{"name": "User"}],
        })

    def test_type_interface_fields_and_possible_types(self):
        data = self.data(
            '{ __type(name: "Node") { kind name fields { name }'
            " interfaces { name } possibleTypes { name } } }"
        )
        node = data["__type"]
        self.assertEqual(node["kind"], "INTERFACE")
        self.assertEqual([f["name"] for f in node["fields"]], ["id"])
        self.assertEqual(node["interfaces"], [])
        self.assertEqual(node["possibleTypes"], [{"name": "User"}])

    def test_type_object_interfaces(self):
        data = self.data(
            '{ __type(name: "User") { interfaces { name kind } } }'
        )
        self.assertEqual(data["__type"]["interfaces"],
                         [{"name": "Node", "kind": "INTERFACE"}])

    def test_type_custom_scalar(self):
        data = self.data('{ __type(name: "DateTime") { kind name } }')
        self.assertEqual(data["__type"],
                         {"kind": "SCALAR", "name": "DateTime"})

    def test_type_meta_types_are_introspectable(self):
        data = self.data(
            '{ a: __type(name: "__Schema") { kind name fields { name } }'
            ' b: __type(name: "__TypeKind") { kind name enumValues { name } }'
            ' c: __type(name: "__DirectiveLocation") { kind enumValues { name } } }'
        )
        self.assertEqual(data["a"]["kind"], "OBJECT")
        self.assertEqual(data["a"]["name"], "__Schema")
        self.assertIn("queryType", [f["name"] for f in data["a"]["fields"]])
        self.assertEqual(data["b"]["kind"], "ENUM")
        self.assertEqual(len(data["b"]["enumValues"]), 8)
        self.assertEqual(data["c"]["kind"], "ENUM")
        self.assertIn("QUERY", [v["name"] for v in data["c"]["enumValues"]])

    def test_type_unknown_name_returns_null(self):
        data = self.data('{ __type(name: "NoSuchType") { name kind } }')
        self.assertIsNone(data["__type"])

    # -- unrequested fields are omitted ----------------------------------------

    def test_only_requested_fields_returned(self):
        data = self.data('{ __type(name: "Role") { name } }')
        self.assertEqual(data["__type"], {"name": "Role"})

    # -- __typename -------------------------------------------------------------

    def test_root_typename(self):
        data = self.data("{ __typename }")
        self.assertEqual(data, {"__typename": "Query"})

    def test_nested_typename_on_entity(self):
        data = self.data(
            "{ users { __typename id t: __typename } }", events=EVENT
        )
        self.assertEqual(data["users"],
                         [{"__typename": "User", "id": 1, "t": "User"}])

    def test_typename_inside_introspection(self):
        data = self.data(
            "{ __schema { __typename queryType { __typename name } } }"
        )
        self.assertEqual(data["__schema"], {
            "__typename": "__Schema",
            "queryType": {"__typename": "__Type", "name": "Query"},
        })

    def test_typename_in_fragment_on_entity(self):
        query = (
            "{ users { ...Bits } }\n"
            "fragment Bits on User { __typename name }\n"
        )
        data = self.data(query, events=EVENT)
        self.assertEqual(data["users"][0]["__typename"], "User")

    # -- aliases / fragments ----------------------------------------------------

    def test_introspection_aliases(self):
        data = self.data(
            '{ q: __schema { queryType { name } }'
            ' u: __type(name: "User") { name } }'
        )
        self.assertEqual(data["q"]["queryType"], {"name": "Query"})
        self.assertEqual(data["u"], {"name": "User"})

    def test_introspection_fragment_spread(self):
        query = (
            "{ __schema { ...S } }\n"
            "fragment S on __Schema { queryType { name } }\n"
        )
        data = self.data(query)
        self.assertEqual(data["__schema"]["queryType"], {"name": "Query"})

    def test_introspection_inline_fragment(self):
        data = self.data(
            "{ __type(name: \"User\") { ... on __Type { name kind } } }"
        )
        self.assertEqual(data["__type"], {"name": "User", "kind": "OBJECT"})

    # -- mixing introspection with entity roots ---------------------------------

    def test_mixed_introspection_and_entity_roots(self):
        data = self.data(
            '{ __schema { queryType { name } } users { id }'
            ' me: __type(name: "User") { kind } }',
            events=EVENT,
        )
        self.assertEqual(data["__schema"]["queryType"], {"name": "Query"})
        self.assertEqual(data["users"], [{"id": 1}])
        self.assertEqual(data["me"], {"kind": "OBJECT"})

    # -- events handling --------------------------------------------------------

    def test_pure_introspection_requires_events_file_but_ignores_contents(self):
        code, stdout, stderr = self.run_cli(
            "{ __schema { queryType { name } } }", events=BROKEN_EVENTS
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["data"]["__schema"]["queryType"],
                         {"name": "Query"})

    def test_pure_typename_ignores_events(self):
        code, stdout, stderr = self.run_cli("{ __typename }",
                                            events=BROKEN_EVENTS)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["data"], {"__typename": "Query"})

    def test_mixed_query_still_parses_events(self):
        self.assert_error(
            "{ __schema { queryType { name } } users { id } }",
            "EventError", events=BROKEN_EVENTS,
        )

    def test_missing_events_file_is_io_error_even_for_introspection(self):
        schema_path = self._write("s.graphql", SCHEMA)
        query_path = self._write("q.graphql",
                                 "{ __schema { queryType { name } } }")
        variables_path = self._write("v.json", "{}")
        argv = ["query-exec", "--schema", schema_path, "--query", query_path,
                "--variables", variables_path, "--events", "/nonexistent/e"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.getvalue())["code"], "IoError")

    # -- variable name argument -------------------------------------------------

    def test_name_from_variable(self):
        query = "query ($n: String) { __type(name: $n) { name } }"
        data = self.data(query, variables='{"n": "User"}')
        self.assertEqual(data["__type"], {"name": "User"})

    def test_name_variable_default(self):
        query = 'query ($n: String = "Role") { __type(name: $n) { name } }'
        data = self.data(query, variables="{}")
        self.assertEqual(data["__type"], {"name": "Role"})

    def test_name_missing_variable_is_variables_error(self):
        query = "query ($n: String!) { __type(name: $n) { name } }"
        self.assert_error(query, "VariablesError")

    def test_name_undeclared_variable_is_variables_error(self):
        self.assert_error('{ __type(name: $n) { name } }', "VariablesError")

    def test_name_wrong_type_variable_is_variables_error(self):
        query = "query ($n: Int) { __type(name: $n) { name } }"
        self.assert_error(query, "VariablesError", variables='{"n": 5}')

    def test_name_null_non_null_variable_is_variables_error(self):
        query = "query ($n: String!) { __type(name: $n) { name } }"
        self.assert_error(query, "VariablesError", variables='{"n": null}')

    # -- InvalidQuery validation ------------------------------------------------

    def test_type_missing_name_argument(self):
        self.assert_error("{ __type { name } }", "InvalidQuery")

    def test_type_name_enum_literal(self):
        self.assert_error('{ __type(name: User) { name } }', "InvalidQuery")

    def test_type_name_int_literal(self):
        self.assert_error('{ __type(name: 1) { name } }', "InvalidQuery")

    def test_type_name_bool_literal(self):
        self.assert_error('{ __type(name: true) { name } }', "InvalidQuery")

    def test_schema_takes_no_arguments(self):
        self.assert_error(
            '{ __schema(x: 1) { queryType { name } } }', "InvalidQuery"
        )

    def test_type_unknown_argument(self):
        self.assert_error(
            '{ __type(name: "X", y: 1) { name } }', "InvalidQuery"
        )

    def test_schema_requires_selection_set(self):
        self.assert_error("{ __schema }", "InvalidQuery")

    def test_type_requires_selection_set(self):
        self.assert_error('{ __type(name: "X") }', "InvalidQuery")

    def test_meta_field_unknown_argument_is_invalid_query(self):
        self.assert_error(
            '{ __schema { types(bogus: 1) { name } } }', "InvalidQuery"
        )

    def test_include_deprecated_wrong_literal_type(self):
        self.assert_error(
            '{ __schema { types { fields(includeDeprecated: 1) { name } } } }',
            "InvalidQuery",
        )

    def test_schema_selected_on_nested_entity(self):
        self.assert_error(
            "{ users { __schema { queryType { name } } } }", "InvalidQuery",
            events=EVENT,
        )

    def test_type_selected_on_nested_entity(self):
        self.assert_error(
            '{ users { __type(name: "X") { name } } }', "InvalidQuery",
            events=EVENT,
        )

    def test_schema_selected_inside_introspection_tree(self):
        self.assert_error(
            "{ __schema { queryType { __schema { queryType { name } } } } }",
            "InvalidQuery",
        )

    def test_typename_takes_no_arguments_at_root(self):
        self.assert_error("{ __typename(x: 1) }", "InvalidQuery")

    def test_typename_takes_no_arguments_nested(self):
        self.assert_error(
            "{ users { __typename(x: 1) } }", "InvalidQuery", events=EVENT
        )

    def test_typename_no_selection_set(self):
        self.assert_error(
            "{ users { __typename { x } } }", "InvalidQuery", events=EVENT
        )

    def test_scalar_meta_field_with_selection(self):
        self.assert_error(
            '{ __schema { queryType { name { x } } } }', "InvalidQuery"
        )

    def test_unknown_meta_field_keeps_unknown_field_code(self):
        self.assert_error(
            '{ __type(name: "X") { bogus } }', "UnknownField"
        )

    # -- operation selection ----------------------------------------------------

    def test_named_query_operation(self):
        query = (
            "query A { __type(name: \"User\") { name } }\n"
            "query B { __schema { queryType { name } } }\n"
        )
        data = self.data(query, operation="B")
        self.assertIn("__schema", data)

    def test_mutation_operation_still_unsupported(self):
        self.assert_error(
            'mutation { __schema { queryType { name } } }',
            "UnsupportedOperation",
        )

    # -- complexity -------------------------------------------------------------

    def test_introspection_available_with_control_off(self):
        data = self.data("{ __schema { queryType { name } } }", limit=None)
        self.assertEqual(data["__schema"]["queryType"], {"name": "Query"})

    def test_typename_available_with_control_off(self):
        data = self.data("{ users { __typename id } }", limit=None,
                         events=EVENT)
        self.assertEqual(data["users"][0]["__typename"], "User")

    def test_introspection_costs_zero_under_strict_limit(self):
        data = self.data(
            '{ __schema { types { fields { name } } } }', limit=1
        )
        self.assertTrue(data["__schema"]["types"])

    def test_typename_costs_zero_under_strict_limit(self):
        # user(id:1) is a single-object root: its one leaf costs 1 and the two
        # __typename selections cost 0, so a limit of 1 still allows it.
        query = "{ user(id: 1) { __typename id t: __typename } }"
        data = self.data(query, limit=1, events=EVENT)
        self.assertEqual(data["user"],
                         {"__typename": "User", "id": 1, "t": "User"})

    def test_entity_part_still_counts_alongside_introspection(self):
        # The entity leaf costs 1; introspection costs 0. A limit of 0 means
        # "disabled", so use limit 1 (allowed) and prove an extra entity leaf
        # trips the existing over-limit path.
        code, stdout, stderr = self.run_cli(
            "{ __schema { queryType { name } } users { id name } }",
            limit=1, events=EVENT,
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        self.assertEqual(
            payload["errors"][0]["extensions"]["code"],
            "QUERY_COMPLEXITY_EXCEEDED",
        )


if __name__ == "__main__":
    unittest.main()
