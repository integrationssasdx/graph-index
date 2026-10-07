"""End-to-end tests for GraphQL introspection in `graph-index query-exec`.

Covers ``__schema`` / ``__type(name:)`` metadata, ``__typename`` in entity
selection sets, the fixed validation error codes (InvalidQuery vs
VariablesError), the zero complexity cost and the "events file readable but
not parsed" rule for introspection-only queries.
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
schema {
  query: QueryRoot
}

type QueryRoot {
  users(id: ID, status: Status = ACTIVE): [User!]!
  user(id: ID!): User
}

enum Status {
  ACTIVE
  IDLE
}

interface Node {
  id: ID!
}

type User implements Node @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: Status
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  reviews: [Review!]! @link(local: "id", target: "user_id")
}

type Team implements Node @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
}

union Owner = User | Team

input UserFilter {
  active: Boolean = true
  tags: [String!] = ["a", "b"]
}

scalar DateTime

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  score: Int
}
"""

EVENTS = "\n".join(
    json.dumps(row)
    for row in [
        {"op": "INSERT", "entity": "teams", "before": None,
         "after": {"id": 9, "name": "core"}},
        {"op": "INSERT", "entity": "reviews", "before": None,
         "after": {"id": "r1", "user_id": 1, "score": 10}},
        {"op": "INSERT", "entity": "users", "before": None,
         "after": {"id": 1, "name": "ada", "status": "ACTIVE",
                   "team_id": 9}},
    ]
) + "\n"


class IntrospectionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def run_cli(self, query, variables="{}", events=EVENTS, schema=SCHEMA,
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

    def assert_data(self, code, stdout, stderr):
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertNotIn("errors", payload)
        return payload["data"]

    def assert_invalid(self, code, stdout, stderr):
        self.assertEqual(code, 2, stdout)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "InvalidQuery")
        return payload["message"]

    def assert_variables_error(self, code, stdout, stderr):
        self.assertEqual(code, 2, stdout)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "VariablesError")

    # -- __schema -------------------------------------------------------------

    def test_schema_root_types(self):
        data = self.assert_data(*self.run_cli(
            "{ __schema { queryType { name } mutationType { name } "
            "subscriptionType { name } } }"))
        self.assertEqual(data["__schema"], {
            "queryType": {"name": "QueryRoot"},
            "mutationType": None,
            "subscriptionType": None,
        })

    def test_schema_unrequested_fields_absent(self):
        data = self.assert_data(*self.run_cli(
            "{ __schema { queryType { kind name } } }"))
        schema = data["__schema"]
        self.assertEqual(set(schema.keys()), {"queryType"})
        self.assertEqual(set(schema["queryType"].keys()), {"kind", "name"})

    def test_schema_types_cover_all_kinds(self):
        data = self.assert_data(*self.run_cli(
            "{ __schema { types { kind name } } }"))
        types = {(t["kind"], t["name"]) for t in data["__schema"]["types"]}
        self.assertIn(("OBJECT", "User"), types)
        self.assertIn(("OBJECT", "QueryRoot"), types)
        self.assertIn(("INTERFACE", "Node"), types)
        self.assertIn(("UNION", "Owner"), types)
        self.assertIn(("ENUM", "Status"), types)
        self.assertIn(("INPUT_OBJECT", "UserFilter"), types)
        self.assertIn(("SCALAR", "DateTime"), types)
        for scalar in ("Int", "Float", "String", "Boolean", "ID"):
            self.assertIn(("SCALAR", scalar), types)
        for meta in ("__Schema", "__Type", "__Field", "__InputValue",
                     "__EnumValue", "__Directive"):
            self.assertIn(("OBJECT", meta), types)
        self.assertIn(("ENUM", "__TypeKind"), types)
        self.assertIn(("ENUM", "__DirectiveLocation"), types)

    def test_schema_directives(self):
        data = self.assert_data(*self.run_cli(
            "{ __schema { directives { name locations isRepeatable "
            "args { name type { kind name ofType { kind name "
            "ofType { kind name } } } defaultValue } } } }"))
        directives = {d["name"]: d for d in data["__schema"]["directives"]}
        self.assertEqual(set(directives), {"skip", "include", "deprecated"})
        self.assertEqual(
            directives["skip"]["locations"],
            ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"])
        self.assertFalse(directives["skip"]["isRepeatable"])
        arg = directives["skip"]["args"][0]
        self.assertEqual(arg["name"], "if")
        self.assertEqual(arg["defaultValue"], None)
        self.assertEqual(arg["type"], {
            "kind": "NON_NULL", "name": None,
            "ofType": {"kind": "SCALAR", "name": "Boolean",
                       "ofType": None},
        })

    # -- __type ---------------------------------------------------------------

    def test_type_object(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "User") { kind name interfaces { name } } }'))
        self.assertEqual(data["__type"], {
            "kind": "OBJECT", "name": "User",
            "interfaces": [{"name": "Node"}],
        })

    def test_type_fields_with_args_and_wrappers(self):
        query = (
            '{ __type(name: "QueryRoot") { fields { name args { name '
            'type { kind name ofType { kind name } } defaultValue } '
            'type { kind name ofType { kind name ofType { kind name '
            'ofType { kind name ofType { kind name } } } } } } } }'
        )
        data = self.assert_data(*self.run_cli(query))
        users = next(
            f for f in data["__type"]["fields"] if f["name"] == "users"
        )
        # [User!]! -> NON_NULL(LIST(NON_NULL(OBJECT User)))
        self.assertEqual(users["type"], {
            "kind": "NON_NULL", "name": None,
            "ofType": {
                "kind": "LIST", "name": None,
                "ofType": {
                    "kind": "NON_NULL", "name": None,
                    "ofType": {"kind": "OBJECT", "name": "User",
                               "ofType": None},
                },
            },
        })
        status_arg = next(a for a in users["args"] if a["name"] == "status")
        self.assertEqual(status_arg["type"], {
            "kind": "ENUM", "name": "Status", "ofType": None})
        self.assertEqual(status_arg["defaultValue"], "ACTIVE")

    def test_type_enum_values(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "Status") { kind name enumValues { name '
            'isDeprecated deprecationReason } } }'))
        values = data["__type"]["enumValues"]
        self.assertEqual([v["name"] for v in values], ["ACTIVE", "IDLE"])
        self.assertFalse(values[0]["isDeprecated"])
        self.assertIsNone(values[0]["deprecationReason"])

    def test_type_interface_possible_types(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "Node") { kind name fields { name } '
            'possibleTypes { name } } }'))
        t = data["__type"]
        self.assertEqual(t["kind"], "INTERFACE")
        self.assertEqual([f["name"] for f in t["fields"]], ["id"])
        self.assertEqual(
            {p["name"] for p in t["possibleTypes"]}, {"User", "Team"})

    def test_type_union_possible_types(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "Owner") { kind possibleTypes { name } } }'))
        self.assertEqual(data["__type"]["kind"], "UNION")
        self.assertEqual(
            {p["name"] for p in data["__type"]["possibleTypes"]},
            {"User", "Team"})

    def test_type_input_fields_and_default_values(self):
        query = (
            '{ __type(name: "UserFilter") { kind inputFields { name '
            'type { kind name ofType { kind name ofType { kind name '
            'ofType { kind name } } } } defaultValue } } }'
        )
        data = self.assert_data(*self.run_cli(query))
        fields = {f["name"]: f for f in data["__type"]["inputFields"]}
        self.assertEqual(fields["active"]["defaultValue"], "true")
        self.assertEqual(fields["tags"]["defaultValue"], '["a", "b"]')
        # [String!] -> LIST(NON_NULL(SCALAR String))
        self.assertEqual(fields["tags"]["type"], {
            "kind": "LIST", "name": None,
            "ofType": {
                "kind": "NON_NULL", "name": None,
                "ofType": {"kind": "SCALAR", "name": "String",
                           "ofType": None},
            },
        })

    def test_type_scalar_null_relationships(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "Int") { kind name fields { name } '
            'interfaces { name } possibleTypes { name } '
            'enumValues { name } inputFields { name } ofType { name } } }'))
        t = data["__type"]
        self.assertEqual((t["kind"], t["name"]), ("SCALAR", "Int"))
        for key in ("fields", "interfaces", "possibleTypes",
                    "enumValues", "inputFields", "ofType"):
            self.assertIsNone(t[key])

    def test_type_meta_type_and_meta_enum(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "__Type") { kind fields { name } } }'))
        names = [f["name"] for f in data["__type"]["fields"]]
        self.assertIn("ofType", names)
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "__TypeKind") { kind enumValues { name } } }'))
        self.assertEqual(data["__type"]["kind"], "ENUM")
        kinds = {v["name"] for v in data["__type"]["enumValues"]}
        self.assertEqual(
            kinds,
            {"SCALAR", "OBJECT", "INTERFACE", "UNION", "ENUM",
             "INPUT_OBJECT", "LIST", "NON_NULL"})

    def test_type_unknown_name_is_null(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "Nope") { name kind } }'))
        self.assertIsNone(data["__type"])

    def test_type_aliased(self):
        data = self.assert_data(*self.run_cli(
            '{ t: __type(name: "Team") { name } }'))
        self.assertEqual(data["t"], {"name": "Team"})

    def test_type_name_from_variable(self):
        query = "query ($n: String!) { __type(name: $n) { kind name } }"
        data = self.assert_data(*self.run_cli(query, '{"n": "Review"}'))
        self.assertEqual(data["__type"], {"kind": "OBJECT", "name": "Review"})

    def test_type_name_variable_default(self):
        query = 'query ($n: String = "Team") { __type(name: $n) { name } }'
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data["__type"], {"name": "Team"})

    def test_schema_fragments(self):
        query = (
            "{ __schema { ...Root } }\n"
            "fragment Root on __Schema { queryType { ...Type } }\n"
            "fragment Type on __Type { name }\n"
        )
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data["__schema"]["queryType"],
                         {"name": "QueryRoot"})

    def test_introspection_typename(self):
        data = self.assert_data(*self.run_cli(
            "{ __schema { __typename queryType { __typename name } } }"))
        self.assertEqual(data["__schema"]["__typename"], "__Schema")
        self.assertEqual(data["__schema"]["queryType"]["__typename"],
                         "__Type")

    # -- __typename on entity selections -------------------------------------

    def test_typename_on_entities(self):
        query = (
            "{ users { __typename id team { __typename id name } "
            "reviews { __typename id } } }"
        )
        data = self.assert_data(*self.run_cli(query))
        user = data["users"][0]
        self.assertEqual(user["__typename"], "User")
        self.assertEqual(user["team"]["__typename"], "Team")
        self.assertEqual(user["reviews"][0]["__typename"], "Review")

    def test_typename_alias(self):
        data = self.assert_data(*self.run_cli(
            "{ users { kind: __typename id } }"))
        self.assertEqual(data["users"][0]["kind"], "User")

    def test_typename_on_root(self):
        data = self.assert_data(*self.run_cli(
            "{ __typename users { id } }"))
        self.assertEqual(data["__typename"], "QueryRoot")

    def test_typename_in_fragment(self):
        query = (
            "{ users { ...Bits } }\n"
            "fragment Bits on Node { __typename id }\n"
        )
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(data["users"][0]["__typename"], "User")

    # -- mixed introspection + entity roots -----------------------------------

    def test_mixed_introspection_and_entities(self):
        data = self.assert_data(*self.run_cli(
            '{ __schema { queryType { name } } users { id __typename } '
            '__type(name: "Team") { name } }'))
        self.assertEqual(data["__schema"]["queryType"]["name"], "QueryRoot")
        self.assertEqual(data["users"][0]["__typename"], "User")
        self.assertEqual(data["__type"], {"name": "Team"})

    # -- events handling ------------------------------------------------------

    def test_pure_introspection_does_not_parse_events(self):
        # Malformed events are ignored entirely; the file only has to exist.
        data = self.assert_data(
            *self.run_cli(
                "{ __schema { queryType { name } } }", events="{not json\n"))
        self.assertEqual(data["__schema"]["queryType"]["name"], "QueryRoot")

    def test_pure_typename_does_not_parse_events(self):
        data = self.assert_data(
            *self.run_cli("{ __typename }", events="{not json\n"))
        self.assertEqual(data["__typename"], "QueryRoot")

    def test_events_file_still_required(self):
        schema_path = self._write("schema.graphql", SCHEMA)
        query_path = self._write("query.graphql",
                                 "{ __schema { queryType { name } } }")
        variables_path = self._write("variables.json", "{}")
        argv = ["query-exec", "--schema", schema_path, "--query", query_path,
                "--variables", variables_path,
                "--events", "/nonexistent/events.ndjson"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["code"], "IoError")

    def test_mixed_query_still_parses_events(self):
        code, _out, stderr = self.run_cli(
            "{ __schema { queryType { name } } users { id } }",
            events="{not json\n")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "EventError")

    # -- complexity -----------------------------------------------------------

    def test_introspection_costs_zero_at_limit_one(self):
        payload = json.loads(self.run_cli(
            "{ __schema { types { name fields { name } } "
            "directives { name args { name } } } }", limit=1)[1])
        self.assertNotIn("errors", payload)

    def test_typename_costs_zero(self):
        payload = json.loads(self.run_cli(
            "{ users { __typename id } }", limit=1000)[1])
        self.assertNotIn("errors", payload)
        self.assertEqual(
            payload["data"]["users"][0]["__typename"], "User")

    def test_introspection_available_without_complexity_control(self):
        data = self.assert_data(*self.run_cli(
            '{ __type(name: "User") { name } }'))
        self.assertEqual(data["__type"], {"name": "User"})

    # -- InvalidQuery cases ---------------------------------------------------

    def test_type_missing_name_argument(self):
        self.assert_invalid(*self.run_cli("{ __type { name } }"))

    def test_type_name_not_a_string(self):
        for literal in ("123", "true", "User", "null", "1.5"):
            self.assert_invalid(
                *self.run_cli(f'{{ __type(name: {literal}) {{ name }} }}'))

    def test_schema_takes_no_arguments(self):
        self.assert_invalid(
            *self.run_cli('{ __schema(x: 1) { queryType { name } } }'))

    def test_type_undeclared_argument(self):
        self.assert_invalid(
            *self.run_cli('{ __type(name: "User", x: 1) { name } }'))

    def test_meta_field_undeclared_argument(self):
        self.assert_invalid(*self.run_cli(
            '{ __type(name: "User") { fields(bogus: true) { name } } }'))

    def test_meta_boolean_argument_wrong_literal(self):
        self.assert_invalid(*self.run_cli(
            '{ __type(name: "User") { fields(includeDeprecated: "yes") '
            '{ name } } }'))

    def test_introspection_requires_selection_set(self):
        self.assert_invalid(*self.run_cli("{ __schema }"))
        self.assert_invalid(*self.run_cli('{ __type(name: "User") }'))

    def test_meta_leaf_field_rejects_selection_set(self):
        self.assert_invalid(*self.run_cli(
            "{ __schema { queryType { name { x } } } }"))

    def test_meta_object_field_requires_selection_set(self):
        self.assert_invalid(*self.run_cli(
            "{ __schema { queryType } }"))

    def test_unknown_meta_field(self):
        self.assert_invalid(*self.run_cli("{ __schema { nope } }"))
        self.assert_invalid(*self.run_cli(
            '{ __type(name: "User") { nope } }'))

    def test_non_root_schema_and_type(self):
        self.assert_invalid(*self.run_cli(
            "{ users { __schema { queryType { name } } } }"))
        self.assert_invalid(*self.run_cli(
            '{ users { __type(name: "User") { name } } }'))

    def test_typename_rejects_arguments(self):
        self.assert_invalid(*self.run_cli("{ users { __typename(x: 1) } }"))

    def test_typename_rejects_selection_set(self):
        self.assert_invalid(*self.run_cli("{ users { __typename { x } } }"))

    def test_introspection_unknown_fragment(self):
        self.assert_invalid(*self.run_cli(
            "{ __schema { ...Nope } }"))

    def test_introspection_empty_selection(self):
        self.assert_invalid(*self.run_cli("{ __schema { } }"))

    # -- VariablesError cases -------------------------------------------------

    def test_type_name_missing_variable(self):
        self.assert_variables_error(*self.run_cli(
            "query ($n: String!) { __type(name: $n) { name } }"))

    def test_type_name_undeclared_variable(self):
        self.assert_variables_error(*self.run_cli(
            "{ __type(name: $n) { name } }"))

    def test_type_name_variable_wrong_type(self):
        self.assert_variables_error(*self.run_cli(
            "query ($n: Int) { __type(name: $n) { name } }",
            '{"n": 5}'))

    def test_type_name_variable_null_for_non_null(self):
        self.assert_variables_error(*self.run_cli(
            "query ($n: String!) { __type(name: $n) { name } }",
            '{"n": null}'))

    def test_include_deprecated_variable_wrong_type(self):
        self.assert_variables_error(*self.run_cli(
            "query ($b: Int) { __type(name: \"User\") "
            "{ fields(includeDeprecated: $b) { name } } }",
            '{"b": 1}'))

    # -- single data envelope -------------------------------------------------

    def test_stdout_has_single_top_level_data(self):
        code, stdout, stderr = self.run_cli(
            '{ __schema { queryType { name } } __type(name: "User") '
            '{ name } users { __typename } }')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.count('"data"'), 1)
        payload = json.loads(stdout)
        self.assertEqual(set(payload), {"data"})


if __name__ == "__main__":
    unittest.main()
