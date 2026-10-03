"""Recursive-descent parser for a useful subset of GraphQL.

One parser handles both SDL and executable documents because the two share
their lexical and value grammar and differ only at the definition level.
The grammar follows https://spec.graphql.org/June2018/ closely enough to
reject malformed input with position-bearing :class:`ParseError`s while
accepting all ordinary mapping schemas and queries.
"""

from . import ast_nodes as ast
from .errors import ParseError
from .lexer import tokenize

_OPERATION_KEYWORDS = {"query", "mutation", "subscription"}


class Parser:
    def __init__(self, source: str):
        self.tokens = tokenize(source)
        self.idx = 0

    # -- Token helpers ------------------------------------------------------

    @property
    def tok(self):
        return self.tokens[self.idx]

    def advance_tok(self):
        t = self.tokens[self.idx]
        if t.kind != "eof":
            self.idx += 1
        return t

    def expect_punct(self, p):
        t = self.tok
        if t.kind != "punct" or t.value != p:
            self._fail(f"expected {p!r}")
        self.advance_tok()
        return t

    def expect_name(self, *allowed):
        t = self.tok
        if t.kind != "name" or (allowed and t.value not in allowed):
            wanted = "/".join(allowed) if allowed else "a name"
            self._fail(f"expected {wanted}")
        self.advance_tok()
        return t

    def at_punct(self, p):
        t = self.tok
        return t.kind == "punct" and t.value == p

    def at_name(self, *names):
        t = self.tok
        return t.kind == "name" and (not names or t.value in names)

    def _fail(self, what):
        t = self.tok
        if t.kind == "eof":
            raise ParseError(f"{what} but reached end of document "
                             f"(line {t.line}, column {t.col})")
        raise ParseError(f"{what} but found {t.value!r} "
                         f"at line {t.line}, column {t.col}")

    # -- Document -----------------------------------------------------------

    def parse_document(self):
        defs = []
        while self.tok.kind != "eof":
            defs.append(self.parse_definition())
        return ast.Document(defs)

    def parse_definition(self):
        # Executable shorthand: a brace at top level starts an anonymous query.
        if self.at_punct("{"):
            return self.parse_operation(after_keyword=False)
        if self.at_name("query", "mutation", "subscription"):
            return self.parse_operation(after_keyword=True)
        if self.at_name("fragment"):
            return self.parse_fragment_definition()
        return self.parse_type_system_definition()

    # -- Operations / fragments --------------------------------------------

    def parse_operation(self, after_keyword):
        if after_keyword:
            kw = self.advance_tok()
            operation = kw.value
            name = None
            if self.tok.kind == "name":
                name = ast.Name(self.tok.value, self.tok.line, self.tok.col)
                self.advance_tok()
            var_defs = []
            if self.at_punct("("):
                var_defs = self.parse_variable_definitions()
            directives = self.parse_directives()
            selection_set = self.parse_selection_set()
            return ast.OperationDefinition(operation, name, var_defs,
                                           directives, selection_set)
        # Anonymous query shorthand.
        selection_set = self.parse_selection_set()
        return ast.OperationDefinition(
            "query", None, [], [], selection_set)

    def parse_variable_definitions(self):
        self.expect_punct("(")
        defs = []
        while not self.at_punct(")"):
            self.expect_punct("$")
            var_tok = self.expect_name()
            variable = ast.Name(var_tok.value, var_tok.line, var_tok.col)
            self.expect_punct(":")
            vtype = self.parse_type()
            default = None
            if self.at_punct("="):
                self.advance_tok()
                default = self.parse_value(is_const=True)
            directives = self.parse_directives()
            defs.append(ast.VariableDefinition(variable, vtype, default,
                                               directives))
        self.expect_punct(")")
        return defs

    def parse_type(self):
        if self.at_punct("["):
            self.advance_tok()
            inner = self.parse_type()
            self.expect_punct("]")
            result = ast.ListType(inner)
        else:
            t = self.expect_name()
            result = ast.NamedType(ast.Name(t.value, t.line, t.col))
        if self.at_punct("!"):
            self.advance_tok()
            result = ast.NonNullType(result)
        return result

    def parse_fragment_definition(self):
        self.expect_name("fragment")
        name_tok = self.expect_name()
        if name_tok.value == "on":
            self._fail("fragment cannot be named 'on'")
        self.expect_name("on")
        cond_tok = self.expect_name()
        directives = self.parse_directives()
        selection_set = self.parse_selection_set()
        return ast.FragmentDefinition(
            ast.Name(name_tok.value, name_tok.line, name_tok.col),
            cond_tok.value, directives, selection_set)

    def parse_selection_set(self):
        self.expect_punct("{")
        selections = []
        while not self.at_punct("}"):
            if self.tok.kind == "eof":
                self._fail("expected selection")
            if self.at_punct("..."):
                selections.append(self.parse_fragment())
            else:
                selections.append(self.parse_field())
        self.expect_punct("}")
        return ast.SelectionSet(selections)

    def parse_fragment(self):
        dots = self.expect_punct("...")
        if self.at_name() and self.tok.value != "on":
            name_tok = self.advance_tok()
            directives = self.parse_directives()
            return ast.FragmentSpread(
                ast.Name(name_tok.value, name_tok.line, name_tok.col),
                directives, dots.line, dots.col)
        type_condition = None
        if self.at_name("on"):
            self.advance_tok()
            cond = self.expect_name()
            type_condition = cond.value
        directives = self.parse_directives()
        selection_set = self.parse_selection_set()
        return ast.InlineFragment(type_condition, directives, selection_set)

    def parse_field(self):
        t = self.expect_name()
        alias = None
        name = ast.Name(t.value, t.line, t.col)
        if self.at_punct(":"):
            self.advance_tok()
            alias = name
            real = self.expect_name()
            name = ast.Name(real.value, real.line, real.col)
        arguments = []
        if self.at_punct("("):
            arguments = self.parse_arguments(is_const=False)
        directives = self.parse_directives()
        selection_set = None
        if self.at_punct("{"):
            selection_set = self.parse_selection_set()
        return ast.Field(alias, name, arguments, directives, selection_set,
                         t.line, t.col)

    def parse_arguments(self, is_const):
        self.expect_punct("(")
        args = []
        while not self.at_punct(")"):
            n = self.expect_name()
            self.expect_punct(":")
            value = self.parse_value(is_const=is_const)
            args.append(ast.Argument(
                ast.Name(n.value, n.line, n.col), value))
        self.expect_punct(")")
        return args

    def parse_directives(self):
        directives = []
        while self.at_punct("@"):
            self.advance_tok()
            n = self.expect_name()
            args = []
            if self.at_punct("("):
                args = self.parse_arguments(is_const=True)
            directives.append(ast.Directive(
                ast.Name(n.value, n.line, n.col), args))
        return directives

    # -- Values -------------------------------------------------------------

    def parse_value(self, is_const):
        t = self.tok
        if t.kind == "punct" and t.value == "$":
            if is_const:
                self._fail("variables are not allowed in constant positions")
            self.advance_tok()
            n = self.expect_name()
            return ast.Variable(ast.Name(n.value, n.line, n.col))
        if t.kind == "number":
            self.advance_tok()
            kind, raw = t.value
            return ast.IntValue(raw) if kind == "int" else ast.FloatValue(raw)
        if t.kind == "string":
            self.advance_tok()
            return ast.StringValue(t.value)
        if t.kind == "name":
            self.advance_tok()
            if t.value == "true":
                return ast.BooleanValue(True)
            if t.value == "false":
                return ast.BooleanValue(False)
            if t.value == "null":
                return ast.NullValue()
            return ast.EnumValue(t.value)
        if t.kind == "punct" and t.value == "[":
            self.advance_tok()
            values = []
            while not self.at_punct("]"):
                if self.tok.kind == "eof":
                    self._fail("expected list value")
                values.append(self.parse_value(is_const))
            self.expect_punct("]")
            return ast.ListValue(values)
        if t.kind == "punct" and t.value == "{":
            self.advance_tok()
            fields = []
            while not self.at_punct("}"):
                n = self.expect_name()
                self.expect_punct(":")
                v = self.parse_value(is_const)
                fields.append((n.value, v))
            self.expect_punct("}")
            return ast.ObjectValue(fields)
        self._fail("expected a value")

    # -- SDL ----------------------------------------------------------------

    def parse_type_system_definition(self):
        # Optional leading description (string token).
        description = None
        if self.tok.kind == "string":
            description = self.advance_tok().value

        if self.at_name("type"):
            return self.parse_object_type(description)
        if self.at_name("interface"):
            return self.parse_interface_type(description)
        if self.at_name("input"):
            return self.parse_input_object_type(description)
        if self.at_name("enum"):
            return self.parse_enum_type(description)
        if self.at_name("union"):
            return self.parse_union_type(description)
        if self.at_name("scalar"):
            self.advance_tok()
            n = self.expect_name()
            directives = self.parse_directives()
            return ast.ScalarTypeDefinition(n.value, directives)
        if self.at_name("schema"):
            self.advance_tok()
            directives = self.parse_directives()
            self.expect_punct("{")
            op_types = []
            while not self.at_punct("}"):
                op = self.expect_name("query", "mutation", "subscription")
                colon = self.expect_punct(":")
                tn = self.expect_name()
                op_types.append((op.value, tn.value))
            self.expect_punct("}")
            return ast.SchemaDefinition(directives, op_types)
        if self.at_name("directive"):
            # Parse and discard the body; directive declarations are accepted
            # as valid schema syntax but do not influence the mapping.
            self._skip_directive_definition()
            return ast.DirectiveDefinition("@skip")
        if self.at_name("extend"):
            # Extensions are accepted schema syntax; the mapping merges the
            # underlying definition they carry into the base type.
            self.advance_tok()
            node = self.parse_type_system_definition()
            node.is_extension = True
            return node
        self._fail("expected a type system definition or operation")

    def _skip_directive_definition(self):
        self.expect_name("directive")
        self.expect_punct("@")
        self.expect_name()
        if self.at_punct("("):
            depth = 0
            while True:
                if self.tok.kind == "eof":
                    self._fail("unterminated directive definition")
                if self.at_punct("("):
                    depth += 1
                    self.advance_tok()
                elif self.at_punct(")"):
                    depth -= 1
                    self.advance_tok()
                    if depth == 0:
                        break
                else:
                    self.advance_tok()
        if self.at_name("on"):
            self.advance_tok()
            if self.at_punct("|"):
                self.advance_tok()
            while self.at_name() or self.at_punct("&"):
                self.advance_tok()
                if self.at_punct("&"):
                    self.advance_tok()

    def parse_object_type(self, description):
        self.expect_name("type")
        name_tok = self.expect_name()
        interfaces = []
        directives = []
        if self.at_name("implements"):
            self.advance_tok()
            if self.at_punct("&"):
                self.advance_tok()
            while self.tok.kind == "name":
                interfaces.append(self.advance_tok().value)
                if self.at_punct("&"):
                    self.advance_tok()
                else:
                    break
        directives = self.parse_directives()
        fields = []
        if self.at_punct("{"):
            fields = self.parse_field_definitions()
        return ast.ObjectTypeDefinition(name_tok.value, fields, directives,
                                        interfaces)

    def parse_interface_type(self, description):
        self.expect_name("interface")
        name_tok = self.expect_name()
        directives = self.parse_directives()
        fields = self.parse_field_definitions() if self.at_punct("{") else []
        return ast.InterfaceTypeDefinition(name_tok.value, fields, directives)

    def parse_input_object_type(self, description):
        self.expect_name("input")
        name_tok = self.expect_name()
        directives = self.parse_directives()
        fields = []
        if self.at_punct("{"):
            self.expect_punct("{")
            while not self.at_punct("}"):
                fields.append(self.parse_input_value_definition())
            self.expect_punct("}")
        return ast.InputObjectTypeDefinition(name_tok.value, fields, directives)

    def parse_enum_type(self, description):
        self.expect_name("enum")
        name_tok = self.expect_name()
        directives = self.parse_directives()
        values = []
        if self.at_punct("{"):
            self.expect_punct("{")
            while not self.at_punct("}"):
                if self.tok.kind == "string":
                    self.advance_tok()  # enum value description
                val_directives = self.parse_directives()
                vt = self.expect_name()
                val_directives = self.parse_directives()
                values.append(ast.Name(vt.value, vt.line, vt.col))
            self.expect_punct("}")
        return ast.EnumTypeDefinition(name_tok.value, values, directives)

    def parse_union_type(self, description):
        self.expect_name("union")
        name_tok = self.expect_name()
        directives = self.parse_directives()
        members = []
        if self.at_punct("="):
            self.advance_tok()
            if self.at_punct("|"):
                self.advance_tok()
            while self.tok.kind == "name":
                members.append(self.advance_tok().value)
                if self.at_punct("|"):
                    self.advance_tok()
                else:
                    break
        return ast.UnionTypeDefinition(name_tok.value, directives, members)

    def parse_field_definitions(self):
        self.expect_punct("{")
        fields = []
        while not self.at_punct("}"):
            if self.tok.kind == "string":
                self.advance_tok()  # field description
            fields.append(self.parse_field_definition())
        self.expect_punct("}")
        return fields

    def parse_field_definition(self):
        name_tok = self.expect_name()
        arguments = []
        if self.at_punct("("):
            arguments = self.parse_argument_definitions()
        self.expect_punct(":")
        ftype = self.parse_type()
        directives = self.parse_directives()
        return ast.FieldDefinition(
            ast.Name(name_tok.value, name_tok.line, name_tok.col),
            arguments, ftype, directives)

    def parse_argument_definitions(self):
        self.expect_punct("(")
        defs = []
        while not self.at_punct(")"):
            if self.tok.kind == "string":
                self.advance_tok()
            defs.append(self.parse_input_value_definition())
        self.expect_punct(")")
        return defs

    def parse_input_value_definition(self):
        name_tok = self.expect_name()
        self.expect_punct(":")
        vtype = self.parse_type()
        default = None
        if self.at_punct("="):
            self.advance_tok()
            default = self.parse_value(is_const=True)
        directives = self.parse_directives()
        return ast.InputValueDefinition(
            ast.Name(name_tok.value, name_tok.line, name_tok.col),
            vtype, default, directives)


def parse(source: str) -> ast.Document:
    return Parser(source).parse_document()
