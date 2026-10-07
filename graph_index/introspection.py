"""GraphQL introspection support for ``query-exec``.

Implements the standard GraphQL introspection surface -- the ``__schema``
and ``__type(name: String!)`` root fields and the implicit ``__typename``
field -- on top of the loaded :class:`~graph_index.schema.Schema`.

The module is self-contained:

* a declarative description of the introspection meta-types (``__Schema``,
  ``__Type``, ``__Field``, ``__InputValue``, ``__EnumValue``,
  ``__Directive`` and the two introspection enums);
* a :class:`Registry` mapping every named schema type and type reference to
  a lazy ``__Type`` object, including LIST / NON_NULL wrappers;
* a :class:`IntroCompiler` validating introspection selection sets against
  the meta-type shape and resolving argument variables through the query
  compiler's existing variable rules;
* deterministic renderers that answer a compiled introspection selection.

Deprecation and descriptions are not modeled by the SDL parser, so the
corresponding standard fields are always reported as ``false`` / ``null``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .errors import PlanError
from .gql import (
    EnumLiteral,
    FieldNode,
    FragmentSpread,
    InlineFragment,
    Var,
)
from .schema import named_of

# ---------------------------------------------------------------------------
# Type-reference helpers (same tuple shape as in gql.py)
# ---------------------------------------------------------------------------


def _nt(name: str):
    return ("named", name)


def _list(inner):
    return ("list", inner)


def _non_null(inner):
    return ("non_null", inner)


STRING = _nt("String")
BOOLEAN = _nt("Boolean")

SCALAR_META_TYPES = frozenset({"String", "Boolean", "Int"})
ENUM_META_TYPES = frozenset({"__TypeKind", "__DirectiveLocation"})

SCHEMA_META_NAMES = (
    "__Schema",
    "__Type",
    "__Field",
    "__InputValue",
    "__EnumValue",
    "__Directive",
)
ENUM_META_NAMES = ("__TypeKind", "__DirectiveLocation")
ALL_META_NAMES = SCHEMA_META_NAMES + ENUM_META_NAMES

TYPE_KINDS = (
    "SCALAR",
    "OBJECT",
    "INTERFACE",
    "UNION",
    "ENUM",
    "INPUT_OBJECT",
    "LIST",
    "NON_NULL",
)
DIRECTIVE_LOCATIONS = (
    "QUERY",
    "MUTATION",
    "SUBSCRIPTION",
    "FIELD",
    "FRAGMENT_DEFINITION",
    "FRAGMENT_SPREAD",
    "INLINE_FRAGMENT",
    "VARIABLE_DEFINITION",
    "SCHEMA",
    "SCALAR",
    "OBJECT",
    "FIELD_DEFINITION",
    "ARGUMENT_DEFINITION",
    "INTERFACE",
    "UNION",
    "ENUM",
    "ENUM_VALUE",
    "INPUT_OBJECT",
    "INPUT_FIELD_DEFINITION",
)


def _is_list_ref(type_ref) -> bool:
    if type_ref[0] == "non_null":
        type_ref = type_ref[1]
    return type_ref[0] == "list"


def _is_leaf_ref(type_ref) -> bool:
    name = named_of(type_ref)
    return name in SCALAR_META_TYPES or name in ENUM_META_TYPES


# ---------------------------------------------------------------------------
# Uniform views over user-defined and meta fields / arguments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgView:
    name: str
    type_ref: tuple
    default: Any
    has_default: bool


@dataclass(frozen=True)
class FieldView:
    name: str
    type_ref: tuple
    args: List[ArgView]


@dataclass(frozen=True)
class DirectiveView:
    name: str
    args: List[ArgView]
    repeatable: bool
    locations: List[str]


def _input_value_view(fdef) -> ArgView:
    return ArgView(fdef.name, fdef.type, fdef.default, fdef.has_default)


# ---------------------------------------------------------------------------
# Lazy __Type objects
# ---------------------------------------------------------------------------


class TypeObj:
    """A lazy ``__Type`` value.

    ``source`` determines how its fields resolve:

    * ``("scalar", name)``, ``("enum", name)``, ``("union", name)``,
      ``("input", name)`` name a user-defined type;
    * ``("object", name)`` / ``("interface", name)`` a user object/interface;
    * ``("meta", name)`` one of the introspection types;
    * ``("wrapper", kind)`` is a LIST / NON_NULL wrapper carrying ``of_type``.
    """

    __slots__ = ("kind", "name", "of_type", "source")

    def __init__(self, kind: str, source, name: Optional[str] = None,
                 of_type: Optional["TypeObj"] = None):
        self.kind = kind
        self.name = name
        self.of_type = of_type
        self.source = source


class FieldObj:
    __slots__ = ("view",)

    def __init__(self, view: FieldView):
        self.view = view


class InputValueObj:
    __slots__ = ("view",)

    def __init__(self, view: ArgView):
        self.view = view


class EnumValueObj:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class DirectiveObj:
    __slots__ = ("view",)

    def __init__(self, view: DirectiveView):
        self.view = view


class SchemaObj:
    """Marker value for the single ``__Schema`` root object."""


# ---------------------------------------------------------------------------
# Built-in directives reported by every schema
# ---------------------------------------------------------------------------


BUILTIN_DIRECTIVES = (
    DirectiveView(
        "skip",
        [ArgView("if", _non_null(_nt("Boolean")), None, False)],
        False,
        ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"],
    ),
    DirectiveView(
        "include",
        [ArgView("if", _non_null(_nt("Boolean")), None, False)],
        False,
        ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"],
    ),
    DirectiveView(
        "deprecated",
        [ArgView("reason", _nt("String"), None, False)],
        False,
        ["FIELD_DEFINITION", "ENUM_VALUE"],
    ),
    DirectiveView(
        "specifiedBy",
        [ArgView("url", _non_null(_nt("String")), None, False)],
        False,
        ["SCALAR"],
    ),
)


# ---------------------------------------------------------------------------
# Meta-field descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetaField:
    name: str
    type_ref: tuple
    args: tuple  # tuple of ArgView
    resolve: Callable  # (registry, obj, args) -> value


def _schema_query_type(reg, obj, args):
    return reg.named(reg.schema.roots.get("query")) if reg.schema.roots.get("query") else None


def _schema_mutation_type(reg, obj, args):
    name = reg.schema.roots.get("mutation")
    return reg.named(name) if name else None


def _schema_subscription_type(reg, obj, args):
    name = reg.schema.roots.get("subscription")
    return reg.named(name) if name else None


def _schema_types(reg, obj, args):
    return reg.all_types()


def _schema_directives(reg, obj, args):
    return reg.all_directives()


def _schema_description(reg, obj, args):
    return None


def _type_kind(reg, obj: TypeObj, args):
    return obj.kind


def _type_name(reg, obj: TypeObj, args):
    return obj.name


def _type_description(reg, obj, args):
    return None


def _type_fields(reg, obj: TypeObj, args):
    if obj.kind not in ("OBJECT", "INTERFACE"):
        return None
    return [FieldObj(view) for view in reg.type_field_views(obj)]


def _type_interfaces(reg, obj: TypeObj, args):
    if obj.kind == "OBJECT":
        return [reg.named(name) for name in reg.object_interface_names(obj)]
    if obj.kind == "INTERFACE":
        return [reg.named(name) for name in reg.interface_interface_names(obj)]
    return None


def _type_possible_types(reg, obj: TypeObj, args):
    if obj.kind == "INTERFACE":
        return [reg.named(name) for name in reg.interface_implementers(obj)]
    if obj.kind == "UNION":
        return [reg.named(name) for name in reg.schema.unions[obj.name]]
    return None


def _type_enum_values(reg, obj: TypeObj, args):
    if obj.kind != "ENUM":
        return None
    if obj.name in META_ENUM_VALUES:
        names = META_ENUM_VALUES[obj.name]
    else:
        names = reg.schema.enum_values.get(obj.name, [])
    return [EnumValueObj(name) for name in names]


def _type_input_fields(reg, obj: TypeObj, args):
    if obj.kind != "INPUT_OBJECT":
        return None
    fields = reg.schema.inputs.get(obj.name, {})
    return [InputValueObj(_input_value_view(fdef)) for fdef in fields.values()]


def _type_of_type(reg, obj: TypeObj, args):
    return obj.of_type


def _type_specified_by_url(reg, obj, args):
    return None


def _field_name(reg, obj: FieldObj, args):
    return obj.view.name


def _field_description(reg, obj, args):
    return None


def _field_args(reg, obj: FieldObj, args):
    return [InputValueObj(a) for a in obj.view.args]


def _field_type(reg, obj: FieldObj, args):
    return reg.wrap(obj.view.type_ref)


def _field_is_deprecated(reg, obj, args):
    return False


def _field_deprecation_reason(reg, obj, args):
    return None


def _input_name(reg, obj: InputValueObj, args):
    return obj.view.name


def _input_description(reg, obj, args):
    return None


def _input_type(reg, obj: InputValueObj, args):
    return reg.wrap(obj.view.type_ref)


def _input_default_value(reg, obj: InputValueObj, args):
    if not obj.view.has_default:
        return None
    return print_const_value(obj.view.default)


def _input_is_deprecated(reg, obj, args):
    return False


def _input_deprecation_reason(reg, obj, args):
    return None


def _enum_value_name(reg, obj: EnumValueObj, args):
    return obj.name


def _enum_value_description(reg, obj, args):
    return None


def _enum_value_is_deprecated(reg, obj, args):
    return False


def _enum_value_deprecation_reason(reg, obj, args):
    return None


def _directive_name(reg, obj: DirectiveObj, args):
    return obj.view.name


def _directive_description(reg, obj, args):
    return None


def _directive_locations(reg, obj: DirectiveObj, args):
    return list(obj.view.locations)


def _directive_args(reg, obj: DirectiveObj, args):
    return [InputValueObj(a) for a in obj.view.args]


def _directive_is_repeatable(reg, obj: DirectiveObj, args):
    return obj.view.repeatable


_INCLUDE_DEPRECATED = (
    ArgView("includeDeprecated", _nt("Boolean"), False, True),
)


def _meta(name, type_ref, resolve, args=()):
    return MetaField(name, type_ref, tuple(args), resolve)


# Declarative shape of every introspection object type.
META_MODEL: Dict[str, Dict[str, MetaField]] = {}


def _build_meta_model() -> None:
    type_type = _nt("__Type")
    META_MODEL["__Schema"] = {
        m.name: m
        for m in (
            _meta("description", STRING, _schema_description),
            _meta("queryType", type_type, _schema_query_type),
            _meta("mutationType", type_type, _schema_mutation_type),
            _meta("subscriptionType", type_type, _schema_subscription_type),
            _meta("types", _non_null(_list(_non_null(type_type))), _schema_types),
            _meta(
                "directives",
                _non_null(_list(_non_null(_nt("__Directive")))),
                _schema_directives,
            ),
        )
    }
    META_MODEL["__Type"] = {
        m.name: m
        for m in (
            _meta("kind", _non_null(_nt("__TypeKind")), _type_kind),
            _meta("name", STRING, _type_name),
            _meta("description", STRING, _type_description),
            _meta(
                "fields",
                _list(_non_null(_nt("__Field"))),
                _type_fields,
                _INCLUDE_DEPRECATED,
            ),
            _meta("interfaces", _list(_non_null(type_type)), _type_interfaces),
            _meta("possibleTypes", _list(_non_null(type_type)), _type_possible_types),
            _meta(
                "enumValues",
                _list(_non_null(_nt("__EnumValue"))),
                _type_enum_values,
                _INCLUDE_DEPRECATED,
            ),
            _meta(
                "inputFields",
                _list(_non_null(_nt("__InputValue"))),
                _type_input_fields,
                _INCLUDE_DEPRECATED,
            ),
            _meta("ofType", type_type, _type_of_type),
            _meta("specifiedByURL", STRING, _type_specified_by_url),
        )
    }
    META_MODEL["__Field"] = {
        m.name: m
        for m in (
            _meta("name", _non_null(STRING), _field_name),
            _meta("description", STRING, _field_description),
            _meta(
                "args",
                _non_null(_list(_non_null(_nt("__InputValue")))),
                _field_args,
                _INCLUDE_DEPRECATED,
            ),
            _meta("type", _non_null(type_type), _field_type),
            _meta("isDeprecated", _non_null(_nt("Boolean")), _field_is_deprecated),
            _meta("deprecationReason", STRING, _field_deprecation_reason),
        )
    }
    input_value_fields = (
        _meta("name", _non_null(STRING), _input_name),
        _meta("description", STRING, _input_description),
        _meta("type", _non_null(type_type), _input_type),
        _meta("defaultValue", STRING, _input_default_value),
        _meta("isDeprecated", _non_null(_nt("Boolean")), _input_is_deprecated),
        _meta("deprecationReason", STRING, _input_deprecation_reason),
    )
    META_MODEL["__InputValue"] = {m.name: m for m in input_value_fields}
    META_MODEL["__EnumValue"] = {
        m.name: m
        for m in (
            _meta("name", _non_null(STRING), _enum_value_name),
            _meta("description", STRING, _enum_value_description),
            _meta(
                "isDeprecated",
                _non_null(_nt("Boolean")),
                _enum_value_is_deprecated,
            ),
            _meta("deprecationReason", STRING, _enum_value_deprecation_reason),
        )
    }
    META_MODEL["__Directive"] = {
        m.name: m
        for m in (
            _meta("name", _non_null(STRING), _directive_name),
            _meta("description", STRING, _directive_description),
            _meta(
                "locations",
                _non_null(_list(_non_null(_nt("__DirectiveLocation")))),
                _directive_locations,
            ),
            _meta(
                "args",
                _non_null(_list(_non_null(_nt("__InputValue")))),
                _directive_args,
                _INCLUDE_DEPRECATED,
            ),
            _meta(
                "isRepeatable",
                _non_null(_nt("Boolean")),
                _directive_is_repeatable,
            ),
        )
    }


_build_meta_model()

META_ENUM_VALUES = {
    "__TypeKind": TYPE_KINDS,
    "__DirectiveLocation": DIRECTIVE_LOCATIONS,
}


# ---------------------------------------------------------------------------
# Default-value printer (GraphQL constant-value syntax, rendered as string)
# ---------------------------------------------------------------------------


def print_const_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, EnumLiteral):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(print_const_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{key}: {print_const_value(item)}"
                        for key, item in value.items())
            + "}"
        )
    return str(value)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Registry:
    """Resolves named types and type references to lazy ``__Type`` objects."""

    def __init__(self, schema):
        self.schema = schema
        self._named: Dict[str, TypeObj] = {}
        self._wrapped: Dict[tuple, TypeObj] = {}
        self._all_types: Optional[List[TypeObj]] = None
        self._all_directives: Optional[List[DirectiveObj]] = None

    # -- type lookup ----------------------------------------------------------

    def named(self, name: Optional[str]) -> Optional[TypeObj]:
        if name is None:
            return None
        cached = self._named.get(name)
        if cached is not None:
            return cached
        schema = self.schema
        if name in META_MODEL:
            obj = TypeObj("OBJECT", ("meta", name), name)
        elif name in META_ENUM_VALUES:
            obj = TypeObj("ENUM", ("meta", name), name)
        elif name in schema.scalars:
            obj = TypeObj("SCALAR", ("scalar", name), name)
        elif name in schema.enums:
            obj = TypeObj("ENUM", ("enum", name), name)
        elif name in schema.types:
            obj = TypeObj("OBJECT", ("object", name), name)
        elif name in schema.interface_types:
            obj = TypeObj("INTERFACE", ("interface", name), name)
        elif name in schema.unions:
            obj = TypeObj("UNION", ("union", name), name)
        elif name in schema.inputs:
            obj = TypeObj("INPUT_OBJECT", ("input", name), name)
        else:
            return None
        self._named[name] = obj
        return obj

    def wrap(self, type_ref) -> Optional[TypeObj]:
        tag = type_ref[0]
        if tag == "named":
            return self.named(type_ref[1])
        cached = self._wrapped.get(type_ref)
        if cached is not None:
            return cached
        inner = self.wrap(type_ref[1])
        kind = "LIST" if tag == "list" else "NON_NULL"
        cached = TypeObj(kind, ("wrapper", kind), None, inner)
        self._wrapped[type_ref] = cached
        return cached

    # -- deterministic listings ----------------------------------------------

    def all_types(self) -> List[TypeObj]:
        if self._all_types is None:
            schema = self.schema
            names: List[str] = []
            names.extend(schema.scalar_order)
            names.extend(schema.types.keys())
            names.extend(schema.interface_types.keys())
            names.extend(schema.unions.keys())
            names.extend(schema.enum_values.keys())
            names.extend(schema.inputs.keys())
            names.extend(ALL_META_NAMES)
            self._all_types = [self.named(name) for name in names]
        return list(self._all_types)

    def all_directives(self) -> List[DirectiveObj]:
        if self._all_directives is None:
            views: List[DirectiveView] = list(BUILTIN_DIRECTIVES)
            for name in self.schema.directive_order:
                info = self.schema.directives[name]
                views.append(
                    DirectiveView(
                        name,
                        [_input_value_view(a) for a in info.args.values()],
                        info.repeatable,
                        list(info.locations),
                    )
                )
            self._all_directives = [DirectiveObj(view) for view in views]
        return list(self._all_directives)

    # -- type-shape resolution ------------------------------------------------

    def type_field_views(self, obj: TypeObj) -> List[FieldView]:
        if obj.kind == "OBJECT" and obj.name in META_MODEL:
            return [
                FieldView(mf.name, mf.type_ref, list(mf.args))
                for mf in META_MODEL[obj.name].values()
            ]
        if obj.kind == "OBJECT":
            info = self.schema.types[obj.name]
        else:
            info = self.schema.interface_types[obj.name]
        return [
            FieldView(
                f.name,
                f.type_ref,
                [_input_value_view(a) for a in f.args.values()],
            )
            for f in info.fields.values()
        ]

    def object_interface_names(self, obj: TypeObj) -> List[str]:
        return list(self.schema.types[obj.name].interfaces)

    def interface_interface_names(self, obj: TypeObj) -> List[str]:
        return list(self.schema.interface_types[obj.name].interfaces)

    def interface_implementers(self, obj: TypeObj) -> List[str]:
        return [
            name
            for name, info in self.schema.types.items()
            if obj.name in info.interfaces
        ]


# ---------------------------------------------------------------------------
# Compiled introspection selection tree
# ---------------------------------------------------------------------------


@dataclass
class IntroNode:
    response_key: str
    field_name: str
    args: Dict[str, Any]
    children: Optional[List["IntroNode"]]
    descriptor: Optional[MetaField]  # None for the implicit __typename
    type_name: Optional[str] = None  # rendered __typename value


# ---------------------------------------------------------------------------
# Selection compiler
# ---------------------------------------------------------------------------


class IntroCompiler:
    """Validates introspection selections against the meta-type model."""

    def __init__(self, schema, resolve_value: Callable[[Any], Any],
                 fragments: Optional[Dict[str, Any]] = None):
        self.registry = Registry(schema)
        self._resolve_value = resolve_value
        self.fragments = fragments or {}

    # -- root compilation ------------------------------------------------------

    def compile_schema_root(self, node: FieldNode, path: str) -> List[IntroNode]:
        if node.args:
            raise PlanError(
                "InvalidQuery",
                f"introspection field '__schema' does not take arguments",
            )
        if node.selection_set is None:
            raise PlanError(
                "InvalidQuery", "field '__schema' requires a selection set"
            )
        return self._compile_set(node.selection_set, "__Schema", path)

    def compile_type_root(
        self, node: FieldNode, path: str
    ) -> tuple:
        if node.selection_set is None:
            raise PlanError(
                "InvalidQuery", "field '__type' requires a selection set"
            )
        names = [name for name in node.args if name != "name"]
        if names:
            raise PlanError(
                "InvalidQuery",
                f"unknown argument '{names[0]}' on introspection field '__type'",
            )
        if "name" not in node.args:
            raise PlanError(
                "InvalidQuery",
                "introspection field '__type' requires a 'name' argument",
            )
        name = self._require_string_arg(node.args["name"], "__type", "name")
        tree = self._compile_set(node.selection_set, "__Type", path)
        return name, tree

    # -- fragment expansion (equality matching on the meta types) --------------

    def _expand(self, selections, type_name: str,
                stack: List[str]) -> List[FieldNode]:
        out: List[FieldNode] = []
        index: Dict[str, FieldNode] = {}

        def add(n: FieldNode) -> None:
            key = n.alias or n.name
            existing = index.get(key)
            if existing is None:
                index[key] = FieldNode(
                    n.name, n.alias, dict(n.args),
                    list(n.selection_set) if n.selection_set is not None else None,
                )
                out.append(index[key])
                return
            if existing.name != n.name:
                raise PlanError(
                    "InvalidQuery",
                    f"conflicting fields '{existing.name}' and '{n.name}' share "
                    f"response key '{key}'",
                )
            if (existing.selection_set is None) != (n.selection_set is None):
                raise PlanError(
                    "InvalidQuery",
                    f"conflicting selections for response key '{key}'",
                )
            if existing.selection_set is not None:
                existing.selection_set.extend(n.selection_set)

        for sel in selections:
            if isinstance(sel, FieldNode):
                add(sel)
            elif isinstance(sel, FragmentSpread):
                frag = self.fragments.get(sel.name)
                if frag is None:
                    raise PlanError(
                        "InvalidQuery", f"unknown fragment '{sel.name}'"
                    )
                if sel.name in stack:
                    raise PlanError(
                        "InvalidQuery", f"fragment cycle involving '{sel.name}'"
                    )
                if frag.type_condition == type_name:
                    for n in self._expand(
                        frag.selection_set, type_name, stack + [sel.name]
                    ):
                        add(n)
            elif isinstance(sel, InlineFragment):
                if sel.type_condition is None or sel.type_condition == type_name:
                    for n in self._expand(sel.selection_set, type_name, stack):
                        add(n)
        return out

    # -- selection set compilation --------------------------------------------

    def _compile_set(self, selections, meta_type_name: str,
                     path: str) -> List[IntroNode]:
        if selections is None:
            raise PlanError(
                "InvalidQuery", f"field '{path}' requires a selection set"
            )
        flat = self._expand(selections, meta_type_name, [])
        if not flat:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        nodes: List[IntroNode] = []
        seen = set()
        for node in flat:
            key = node.alias or node.name
            node_path = f"{path}.{key}"
            if node.name == "__typename":
                self.validate_typename(node)
                if key not in seen:
                    seen.add(key)
                    nodes.append(
                        IntroNode(key, "__typename", {}, None, None,
                                  type_name=meta_type_name)
                    )
                continue
            if node.name in ("__schema", "__type"):
                raise PlanError(
                    "InvalidQuery",
                    f"introspection field '{node.name}' may only be selected "
                    f"on the root query type",
                )
            descriptor = META_MODEL[meta_type_name].get(node.name)
            if descriptor is None:
                raise PlanError(
                    "UnknownField",
                    f"unknown field '{node.name}' on type '{meta_type_name}'",
                )
            args = self._compile_args(node, descriptor, meta_type_name)
            leaf = _is_leaf_ref(descriptor.type_ref)
            if leaf:
                if node.selection_set is not None:
                    raise PlanError(
                        "InvalidQuery",
                        f"scalar field '{node.name}' must not have a selection set",
                    )
                children = None
            else:
                if node.selection_set is None:
                    raise PlanError(
                        "InvalidQuery",
                        f"field '{node.name}' requires a selection set",
                    )
                children = self._compile_set(
                    node.selection_set, named_of(descriptor.type_ref), node_path
                )
            if key not in seen:
                seen.add(key)
                nodes.append(IntroNode(key, node.name, args, children, descriptor))
        return nodes

    @staticmethod
    def validate_typename(node: FieldNode) -> None:
        """Validate the implicit __typename field on any object selection."""
        if node.args:
            raise PlanError(
                "InvalidQuery", "field '__typename' does not take arguments"
            )
        if node.selection_set is not None:
            raise PlanError(
                "InvalidQuery", "field '__typename' must not have a selection set"
            )

    def _compile_args(
        self, node: FieldNode, descriptor: MetaField, meta_type_name: str
    ) -> Dict[str, Any]:
        resolved: Dict[str, Any] = {}
        declared = {arg.name: arg for arg in descriptor.args}
        for arg_name, value in node.args.items():
            arg = declared.get(arg_name)
            if arg is None:
                raise PlanError(
                    "InvalidQuery",
                    f"unknown argument '{arg_name}' on introspection field "
                    f"'{node.name}'",
                )
            resolved[arg_name] = self._check_arg_value(value, arg, node.name)
        for arg in descriptor.args:
            if arg.name not in resolved:
                resolved[arg.name] = arg.default if arg.has_default else None
        return resolved

    def _check_arg_value(self, value, arg: ArgView, field_name: str) -> Any:
        if isinstance(value, Var):
            # Variable rules (undeclared, missing or wrong type) keep their
            # existing VariablesError code via the query compiler.
            value = self._resolve_value(value)
            return self._ensure_python_type(value, arg, field_name, True)
        return self._ensure_python_type(value, arg, field_name, False)

    def _ensure_python_type(
        self, value, arg: ArgView, field_name: str, variable: bool
    ) -> Any:
        type_name = named_of(arg.type_ref)
        nullable = arg.type_ref[0] != "non_null"
        if value is None:
            if nullable:
                return None
            self._arg_type_error(arg, field_name, variable)
        if type_name == "Boolean":
            if isinstance(value, bool):
                return value
        elif type_name == "String":
            if isinstance(value, str) and not isinstance(value, EnumLiteral):
                return value
        elif type_name == "Int":
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        self._arg_type_error(arg, field_name, variable)

    def _arg_type_error(self, arg: ArgView, field_name: str,
                        variable: bool) -> None:
        if variable:
            raise PlanError(
                "VariablesError",
                f"variable for argument '{arg.name}' has a value of the wrong type",
            )
        raise PlanError(
            "InvalidQuery",
            f"argument '{arg.name}' on introspection field '{field_name}' "
            f"must be a {named_of(arg.type_ref)} literal",
        )

    def _require_string_arg(self, value, field_name: str, arg_name: str) -> str:
        if isinstance(value, Var):
            resolved = self._resolve_value(value)
            if not isinstance(resolved, str) or isinstance(resolved, EnumLiteral):
                raise PlanError(
                    "VariablesError",
                    f"variable for argument '{arg_name}' must be a String value",
                )
            return resolved
        if isinstance(value, (list, dict)) or not isinstance(value, str) \
                or isinstance(value, EnumLiteral) or isinstance(value, bool):
            raise PlanError(
                "InvalidQuery",
                f"argument '{arg_name}' on introspection field '{field_name}' "
                f"must be a String literal",
            )
        return value


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_children(nodes: List[IntroNode], obj: Any,
                     registry: Registry) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for node in nodes:
        if node.descriptor is None:
            data[node.response_key] = node.type_name
            continue
        value = node.descriptor.resolve(registry, obj, node.args)
        if node.children is None:
            data[node.response_key] = value
            continue
        if value is None:
            data[node.response_key] = None
        elif _is_list_ref(node.descriptor.type_ref):
            data[node.response_key] = [
                _render_children(node.children, item, registry) for item in value
            ]
        else:
            data[node.response_key] = _render_children(
                node.children, value, registry
            )
    return data


def render_schema_root(tree: List[IntroNode], schema) -> Dict[str, Any]:
    registry = Registry(schema)
    return _render_children(tree, SchemaObj(), registry)


def render_type_root(tree: List[IntroNode], name: str, schema) -> Any:
    registry = Registry(schema)
    obj = registry.named(name)
    if obj is None:
        return None
    return _render_children(tree, obj, registry)
