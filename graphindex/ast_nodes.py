"""Plain AST nodes produced by the GraphQL parser.

Both SDL (``schema.graphql``) and executable (``query.graphql``) constructs
are represented here. Only fields actually needed downstream carry rich
types; everything is parsed faithfully so syntax errors are still reported.
"""


# -- Shared primitives -------------------------------------------------------

class Name:
    __slots__ = ("value", "line", "col")

    def __init__(self, value, line=0, col=0):
        self.value = value
        self.line = line
        self.col = col


class Variable:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name  # Name


class NamedType:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name  # Name


class ListType:
    __slots__ = ("type",)

    def __init__(self, type):
        self.type = type


class NonNullType:
    __slots__ = ("type",)

    def __init__(self, type):
        self.type = type  # NamedType | ListType


class Argument:
    __slots__ = ("name", "value")

    def __init__(self, name, value):
        self.name = name
        self.value = value


class Directive:
    __slots__ = ("name", "arguments")

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


# -- Values ------------------------------------------------------------------

class IntValue:
    __slots__ = ("value",)

    def __init__(self, raw):
        self.value = int(raw)


class FloatValue:
    __slots__ = ("value",)

    def __init__(self, raw):
        self.value = float(raw)


class StringValue:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class BooleanValue:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class NullValue:
    value = None


class EnumValue:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class ListValue:
    __slots__ = ("values",)

    def __init__(self, values):
        self.values = values


class ObjectValue:
    __slots__ = ("fields",)

    def __init__(self, fields):
        self.fields = fields  # list[(str, value)]


# -- SDL ---------------------------------------------------------------------

class InputValueDefinition:
    __slots__ = ("name", "type", "default", "directives")

    def __init__(self, name, type, default, directives):
        self.name = name
        self.type = type
        self.default = default
        self.directives = directives


class FieldDefinition:
    __slots__ = ("name", "arguments", "type", "directives")

    def __init__(self, name, arguments, type, directives):
        self.name = name
        self.arguments = arguments  # list[InputValueDefinition]
        self.type = type
        self.directives = directives


class ObjectTypeDefinition:
    def __init__(self, name, fields, directives, interfaces=()):
        self.name = name
        self.fields = fields
        self.directives = directives
        self.interfaces = list(interfaces)


class InterfaceTypeDefinition:
    def __init__(self, name, fields, directives):
        self.name = name
        self.fields = fields
        self.directives = directives


class InputObjectTypeDefinition:
    def __init__(self, name, fields, directives):
        self.name = name
        self.fields = fields
        self.directives = directives


class EnumTypeDefinition:
    def __init__(self, name, values, directives):
        self.name = name
        self.values = values  # list[Name]
        self.directives = directives


class UnionTypeDefinition:
    def __init__(self, name, directives, members):
        self.name = name
        self.directives = directives
        self.members = members  # list[str]


class ScalarTypeDefinition:
    def __init__(self, name, directives):
        self.name = name
        self.directives = directives


class SchemaDefinition:
    def __init__(self, directives, operation_types):
        self.directives = directives
        self.operation_types = operation_types  # list[(str, str)]


class DirectiveDefinition:
    def __init__(self, name):
        self.name = name


# -- Executable --------------------------------------------------------------

class Field:
    __slots__ = ("alias", "name", "arguments", "directives", "selection_set",
                 "line", "col")

    def __init__(self, alias, name, arguments, directives, selection_set,
                 line=0, col=0):
        self.alias = alias  # Name | None
        self.name = name  # Name
        self.arguments = arguments
        self.directives = directives
        self.selection_set = selection_set  # SelectionSet | None
        self.line = line
        self.col = col


class FragmentSpread:
    __slots__ = ("name", "directives", "line", "col")

    def __init__(self, name, directives, line=0, col=0):
        self.name = name
        self.directives = directives
        self.line = line
        self.col = col


class InlineFragment:
    __slots__ = ("type_condition", "directives", "selection_set")

    def __init__(self, type_condition, directives, selection_set):
        self.type_condition = type_condition  # str | None
        self.directives = directives
        self.selection_set = selection_set


class SelectionSet:
    __slots__ = ("selections",)

    def __init__(self, selections):
        self.selections = selections


class VariableDefinition:
    __slots__ = ("variable", "type", "default", "directives")

    def __init__(self, variable, type, default, directives):
        self.variable = variable  # Name
        self.type = type
        self.default = default
        self.directives = directives


class OperationDefinition:
    def __init__(self, operation, name, variable_definitions, directives,
                 selection_set):
        self.operation = operation  # 'query' | 'mutation' | 'subscription'
        self.name = name  # Name | None
        self.variable_definitions = variable_definitions
        self.directives = directives
        self.selection_set = selection_set


class FragmentDefinition:
    def __init__(self, name, type_condition, directives, selection_set):
        self.name = name  # Name
        self.type_condition = type_condition  # str
        self.directives = directives
        self.selection_set = selection_set


class Document:
    def __init__(self, definitions):
        self.definitions = definitions

    def operations(self):
        return [d for d in self.definitions
                if isinstance(d, OperationDefinition)]

    def fragments(self):
        return {d.name.value: d for d in self.definitions
                if isinstance(d, FragmentDefinition)}
