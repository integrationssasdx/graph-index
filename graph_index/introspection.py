"""GraphQL introspection support for ``query-exec``.

Implements the standard introspection root fields ``__schema`` and
``__type(name: String!)`` together with the meta-types they return:
``__Schema`` / ``__Type`` / ``__Field`` / ``__InputValue`` /
``__EnumValue`` / ``__Directive`` and the ``__TypeKind`` /
``__DirectiveLocation`` enums.

A query selection is compiled and validated against the fixed meta-schema
exactly like an ordinary selection (unknown fields, undeclared arguments,
missing or forbidden selection sets all end with ``InvalidQuery``); the
resolver then emits only the requested attributes, so unrequested
relationships never appear in the response.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import PlanError
from .gql import EnumLiteral, FieldNode, FragmentSpread, InlineFragment
from .schema import BUILTIN_SCALARS, Schema

TypeRef = Tuple

# -- compact type-ref shorthand ---------------------------------------------


def _N(name: str) -> TypeRef:
    return ("named", name)


# ---------------------------------------------------------------------------
# Meta-schema definition
# ---------------------------------------------------------------------------

# Meta object type names and the two meta enums.
META_OBJECT_NAMES = (
    "__Schema",
    "__Type",
    "__Field",
    "__InputValue",
    "__EnumValue",
    "__Directive",
)
META_ENUM_NAMES = ("__TypeKind", "__DirectiveLocation")
META_TYPE_NAMES = META_OBJECT_NAMES + META_ENUM_NAMES

TYPE_KIND_VALUES = (
    "SCALAR",
    "OBJECT",
    "INTERFACE",
    "UNION",
    "ENUM",
    "INPUT_OBJECT",
    "LIST",
    "NON_NULL",
)

DIRECTIVE_LOCATION_VALUES = (
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

BUILTIN_SCALAR_ORDER = ("Int", "Float", "String", "Boolean", "ID")

# A meta field definition:
#   (name, attribute, shape, child meta type | None, type ref, args)
# shape is one of:
#   "leaf"    -- scalar/enum scalar, no subselection
#   "object"  -- nullable/wrapped __Type-like object, subselection required
#   "list"    -- list of child meta objects (or null)
#   "strlist" -- list of plain enum strings (or null)
# args: list of (arg name, type ref, has_default, default)

_META_FIELDS: Dict[str, List[tuple]] = {
    "__Schema": [
        ("description", "description", "leaf", None, _N("String"), []),
        ("types", "types", "list", "__Type",
         ("non_null", ("list", ("non_null", _N("__Type")))), []),
        ("queryType", "queryType", "object", "__Type", _N("__Type"), []),
        ("mutationType", "mutationType", "object", "__Type", _N("__Type"), []),
        ("subscriptionType", "subscriptionType", "object", "__Type",
         _N("__Type"), []),
        ("directives", "directives", "list", "__Directive",
         ("non_null", ("list", ("non_null", _N("__Directive")))), []),
    ],
    "__Type": [
        ("kind", "kind", "leaf", None, ("non_null", _N("__TypeKind")), []),
        ("name", "name", "leaf", None, _N("String"), []),
        ("description", "description", "leaf", None, _N("String"), []),
        ("fields", "fields", "list", "__Field",
         ("list", ("non_null", _N("__Field"))),
         [("includeDeprecated", _N("Boolean"), True, False)]),
        ("interfaces", "interfaces", "list", "__Type",
         ("list", ("non_null", _N("__Type"))), []),
        ("possibleTypes", "possibleTypes", "list", "__Type",
         ("list", ("non_null", _N("__Type"))), []),
        ("enumValues", "enumValues", "list", "__EnumValue",
         ("list", ("non_null", _N("__EnumValue"))),
         [("includeDeprecated", _N("Boolean"), True, False)]),
        ("inputFields", "inputFields", "list", "__InputValue",
         ("list", ("non_null", _N("__InputValue"))), []),
        ("ofType", "ofType", "object", "__Type", _N("__Type"), []),
    ],
    "__Field": [
        ("name", "name", "leaf", None, ("non_null", _N("String")), []),
        ("description", "description", "leaf", None, _N("String"), []),
        ("args", "args", "list", "__InputValue",
         ("non_null", ("list", ("non_null", _N("__InputValue")))), []),
        ("type", "type", "object", "__Type", ("non_null", _N("__Type")), []),
        ("isDeprecated", "isDeprecated", "leaf", None,
         ("non_null", _N("Boolean")), []),
        ("deprecationReason", "deprecationReason", "leaf", None,
         _N("String"), []),
    ],
    "__InputValue": [
        ("name", "name", "leaf", None, ("non_null", _N("String")), []),
        ("description", "description", "leaf", None, _N("String"), []),
        ("type", "type", "object", "__Type", ("non_null", _N("__Type")), []),
        ("defaultValue", "defaultValue", "leaf", None, _N("String"), []),
    ],
    "__EnumValue": [
        ("name", "name", "leaf", None, ("non_null", _N("String")), []),
        ("description", "description", "leaf", None, _N("String"), []),
        ("isDeprecated", "isDeprecated", "leaf", None,
         ("non_null", _N("Boolean")), []),
        ("deprecationReason", "deprecationReason", "leaf", None,
         _N("String"), []),
    ],
    "__Directive": [
        ("name", "name", "leaf", None, ("non_null", _N("String")), []),
        ("description", "description", "leaf", None, _N("String"), []),
        ("locations", "locations", "strlist", None,
         ("non_null", ("list", ("non_null", _N("__DirectiveLocation")))), []),
        ("args", "args", "list", "__InputValue",
         ("non_null", ("list", ("non_null", _N("__InputValue")))), []),
        ("isRepeatable", "isRepeatable", "leaf", None,
         ("non_null", _N("Boolean")), []),
    ],
}

_META_FIELD_INDEX: Dict[str, Dict[str, tuple]] = {
    meta: {spec[0]: spec for spec in specs}
    for meta, specs in _META_FIELDS.items()
}

# (name, locations, args, isRepeatable); args are (name, type ref).
_BUILTIN_DIRECTIVES = (
    ("skip",
     ("FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"),
     (("if", ("non_null", _N("Boolean"))),),
     False),
    ("include",
     ("FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"),
     (("if", ("non_null", _N("Boolean"))),),
     False),
    ("deprecated",
     ("FIELD_DEFINITION", "ENUM_VALUE"),
     (("reason", _N("String")),),
     False),
)


# ---------------------------------------------------------------------------
# Default-value serialization (InputValue.defaultValue returns a String)
# ---------------------------------------------------------------------------


def serialize_default(value: Any) -> str:
    """Render a const-parsed value as GraphQL input syntax."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, EnumLiteral):  # must precede str
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(serialize_default(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{key}: {serialize_default(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Descriptor construction
# ---------------------------------------------------------------------------


def _bare_type_descriptor(kind: str, name: Optional[str]) -> dict:
    return {
        "kind": kind,
        "name": name,
        "description": None,
        "fields": None,
        "interfaces": None,
        "possibleTypes": None,
        "enumValues": None,
        "inputFields": None,
        "ofType": None,
    }


class _DescriptorBuilder:
    """Builds ``__Type``/``__Field``/... descriptor dicts for one schema."""

    def __init__(self, schema: Schema):
        self.schema = schema
        self._cache: Dict[str, dict] = {}

    # -- __Schema -------------------------------------------------------------

    def build_schema(self) -> dict:
        names: List[str] = []
        names.extend(self.schema.types.keys())
        names.extend(self.schema.interface_fields.keys())
        names.extend(self.schema.unions.keys())
        names.extend(self.schema.enum_values.keys())
        names.extend(self.schema.inputs.keys())
        names.extend(sorted(self.schema.scalars.difference(BUILTIN_SCALARS)))
        names.extend(META_TYPE_NAMES)
        names.extend(BUILTIN_SCALAR_ORDER)
        return {
            "description": None,
            "types": [self.type_descriptor(name) for name in names],
            "queryType": self._root_type("query"),
            "mutationType": self._root_type("mutation"),
            "subscriptionType": self._root_type("subscription"),
            "directives": [
                self._directive_descriptor(spec) for spec in _BUILTIN_DIRECTIVES
            ],
        }

    def _root_type(self, op: str) -> Optional[dict]:
        name = self.schema.roots.get(op)
        return self.type_descriptor(name) if name is not None else None

    def known_type(self, name: str) -> bool:
        return (
            name in self.schema.types
            or name in self.schema.interface_fields
            or name in self.schema.unions
            or name in self.schema.enum_values
            or name in self.schema.inputs
            or name in self.schema.scalars
            or name in META_TYPE_NAMES
        )

    # -- __Type ---------------------------------------------------------------

    def type_descriptor(self, name: Optional[str]) -> Optional[dict]:
        if name is None:
            return None
        cached = self._cache.get(name)
        if cached is not None:
            return cached

        schema = self.schema
        if name in schema.types:
            desc = _bare_type_descriptor("OBJECT", name)
            self._cache[name] = desc
            info = schema.types[name]
            desc["interfaces"] = [
                self.type_descriptor(iface) for iface in info.interfaces
            ]
            desc["fields"] = [
                self._field_descriptor(f) for f in info.fields.values()
            ]
        elif name in schema.interface_fields:
            desc = _bare_type_descriptor("INTERFACE", name)
            self._cache[name] = desc
            desc["possibleTypes"] = [
                self.type_descriptor(type_name)
                for type_name, info in schema.types.items()
                if name in info.interfaces
            ]
            desc["fields"] = [
                self._field_descriptor(f)
                for f in schema.interface_fields[name].values()
            ]
        elif name in schema.unions:
            desc = _bare_type_descriptor("UNION", name)
            self._cache[name] = desc
            desc["possibleTypes"] = [
                self.type_descriptor(member) for member in schema.unions[name]
            ]
        elif name in schema.enum_values:
            desc = self._enum_descriptor(name)
            self._cache[name] = desc
        elif name in schema.inputs:
            desc = _bare_type_descriptor("INPUT_OBJECT", name)
            self._cache[name] = desc
            desc["inputFields"] = [
                self._input_value_descriptor(f)
                for f in schema.inputs[name].values()
            ]
        elif name in META_OBJECT_NAMES:
            desc = _bare_type_descriptor("OBJECT", name)
            self._cache[name] = desc
            desc["fields"] = [
                self._meta_field_descriptor(spec)
                for spec in _META_FIELDS[name]
            ]
        elif name in META_ENUM_NAMES:
            desc = self._meta_enum_descriptor(name)
            self._cache[name] = desc
        elif name in schema.scalars:
            desc = _bare_type_descriptor("SCALAR", name)
            self._cache[name] = desc
        else:
            return None
        return desc

    def ref_descriptor(self, type_ref: TypeRef) -> dict:
        """Build a descriptor for a parsed type reference (with wrappers)."""
        kind = type_ref[0]
        if kind == "named":
            return self.type_descriptor(type_ref[1])
        wrapper = "LIST" if kind == "list" else "NON_NULL"
        return self._wrapper_descriptor(wrapper, self.ref_descriptor(type_ref[1]))

    def _wrapper_descriptor(self, kind: str, of_type: Optional[dict]) -> dict:
        desc = _bare_type_descriptor(kind, None)
        desc["ofType"] = of_type
        return desc

    def _enum_descriptor(self, name: str) -> dict:
        desc = _bare_type_descriptor("ENUM", name)
        desc["enumValues"] = [self._enum_value_descriptor(v)
                              for v in self.schema.enum_values[name]]
        return desc

    def _meta_enum_descriptor(self, name: str) -> dict:
        desc = _bare_type_descriptor("ENUM", name)
        values = (
            TYPE_KIND_VALUES if name == "__TypeKind" else DIRECTIVE_LOCATION_VALUES
        )
        desc["enumValues"] = [self._enum_value_descriptor(v) for v in values]
        return desc

    # -- __Field / __InputValue / __EnumValue / __Directive -------------------

    def _field_descriptor(self, field) -> dict:
        return {
            "name": field.name,
            "description": None,
            "args": [
                self._input_value_descriptor(arg)
                for arg in field.args.values()
            ],
            "type": self.ref_descriptor(field.type_ref),
            "isDeprecated": False,
            "deprecationReason": None,
        }

    def _meta_field_descriptor(self, spec: tuple) -> dict:
        _name, _attr, _shape, _child, type_ref, args = spec
        return {
            "name": _name,
            "description": None,
            "args": [
                self._arg_descriptor(arg_name, arg_ref, has_default, default)
                for arg_name, arg_ref, has_default, default in args
            ],
            "type": self.ref_descriptor(type_ref),
            "isDeprecated": False,
            "deprecationReason": None,
        }

    def _input_value_descriptor(self, field_def) -> dict:
        return {
            "name": field_def.name,
            "description": None,
            "type": self.ref_descriptor(field_def.type),
            "defaultValue": (
                serialize_default(field_def.default)
                if field_def.has_default
                else None
            ),
        }

    def _arg_descriptor(self, name, type_ref, has_default, default) -> dict:
        return {
            "name": name,
            "description": None,
            "type": self.ref_descriptor(type_ref),
            "defaultValue": serialize_default(default) if has_default else None,
        }

    def _enum_value_descriptor(self, value: str) -> dict:
        return {
            "name": value,
            "description": None,
            "isDeprecated": False,
            "deprecationReason": None,
        }

    def _directive_descriptor(self, spec) -> dict:
        name, locations, args, is_repeatable = spec
        return {
            "name": name,
            "description": None,
            "locations": list(locations),
            "args": [
                self._arg_descriptor(arg_name, arg_ref, False, None)
                for arg_name, arg_ref in args
            ],
            "isRepeatable": is_repeatable,
        }


# ---------------------------------------------------------------------------
# Selection compilation / validation
# ---------------------------------------------------------------------------


def _invalid(message: str) -> PlanError:
    return PlanError("InvalidQuery", message)


def _compile_selections(
    selections,
    meta_type: str,
    fragments: Dict[str, Any],
    stack: List[str],
    coerce_bool: Callable[[Any], bool],
) -> List[tuple]:
    """Validate selections against one meta object type.

    Entries are ``("typename", key)`` or
    ``("field", key, attribute, shape, child meta type, child entries)``.
    Fields sharing a response key are merged exactly as elsewhere.
    """
    out: List[tuple] = []
    index: Dict[str, tuple] = {}

    def add(entry: tuple) -> None:
        key = entry[1]
        existing = index.get(key)
        if existing is None:
            index[key] = entry
            out.append(entry)
            return
        if existing[0] != entry[0] or (
            existing[0] == "field" and existing[2] != entry[2]
        ):
            raise _invalid(
                f"conflicting fields share response key '{key}'"
            )
        if existing[0] == "field" and existing[5] is not None:
            existing[5].extend(entry[5])

    for sel in selections:
        if isinstance(sel, FragmentSpread):
            frag = fragments.get(sel.name)
            if frag is None:
                raise _invalid(f"unknown fragment '{sel.name}'")
            if sel.name in stack:
                raise _invalid(f"fragment cycle involving '{sel.name}'")
            if frag.type_condition != meta_type:
                continue
            for entry in _compile_selections(
                frag.selection_set, meta_type, fragments,
                stack + [sel.name], coerce_bool,
            ):
                add(entry)
            continue
        if isinstance(sel, InlineFragment):
            if sel.type_condition is not None and sel.type_condition != meta_type:
                continue
            for entry in _compile_selections(
                sel.selection_set, meta_type, fragments, stack, coerce_bool
            ):
                add(entry)
            continue

        assert isinstance(sel, FieldNode)
        key = sel.alias or sel.name
        if sel.name == "__typename":
            if sel.args:
                raise _invalid("'__typename' does not take arguments")
            if sel.selection_set is not None:
                raise _invalid("'__typename' must not have a selection set")
            add(("typename", key, None, None, None, None))
            continue

        spec = _META_FIELD_INDEX.get(meta_type, {}).get(sel.name)
        if spec is None:
            raise _invalid(
                f"unknown introspection field '{sel.name}' on type '{meta_type}'"
            )
        _fname, attr, shape, child_meta, _type_ref, arg_defs = spec
        for arg_name, arg_value in sel.args.items():
            declared = next((a for a in arg_defs if a[0] == arg_name), None)
            if declared is None:
                raise _invalid(
                    f"unknown argument '{arg_name}' on introspection field "
                    f"'{_fname}'"
                )
            coerce_bool(arg_value)
        if shape in ("leaf", "strlist"):
            if sel.selection_set is not None:
                raise _invalid(
                    f"introspection field '{_fname}' must not have a selection set"
                )
            add(("field", key, attr, shape, None, None))
            continue
        if sel.selection_set is None:
            raise _invalid(
                f"introspection field '{_fname}' requires a selection set"
            )
        child_entries = _compile_selections(
            sel.selection_set, child_meta, fragments, stack, coerce_bool
        )
        if not child_entries:
            raise _invalid(
                f"introspection field '{_fname}' requires a selection set"
            )
        add(("field", key, attr, shape, child_meta, child_entries))

    if not out:
        raise _invalid("introspection selection set must not be empty")
    return out


def compile_introspection_root(
    node: FieldNode,
    schema: Schema,
    fragments: Dict[str, Any],
    coerce_name: Callable[[Any], str],
    coerce_bool: Callable[[Any], bool],
) -> dict:
    """Validate and compile one root ``__schema`` / ``__type`` field."""
    key = node.alias or node.name
    if node.selection_set is None:
        raise _invalid(
            f"introspection field '{node.name}' requires a selection set"
        )

    type_name: Optional[str] = None
    if node.name == "__schema":
        if node.args:
            raise _invalid("'__schema' does not take arguments")
        meta_type = "__Schema"
    else:
        if "name" not in node.args:
            raise _invalid(
                "'__type' is missing the required argument 'name'"
            )
        for arg_name in node.args:
            if arg_name != "name":
                raise _invalid(
                    f"unknown argument '{arg_name}' on introspection field '__type'"
                )
        type_name = coerce_name(node.args["name"])
        meta_type = "__Type"

    # An unknown type name resolves to null at execution time; the selection
    # is still validated structurally against __Type.
    selections = _compile_selections(
        node.selection_set, meta_type, fragments, [], coerce_bool
    )
    return {
        "kind": "introspection",
        "introspection": node.name,
        "response_key": key,
        "type_name": type_name,
        "selections": selections,
    }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_introspection_root(root: dict, schema: Schema) -> Any:
    builder = _DescriptorBuilder(schema)
    if root["introspection"] == "__schema":
        desc = builder.build_schema()
        meta_type = "__Schema"
    else:
        desc = builder.type_descriptor(root["type_name"])
        if desc is None:
            return None
        meta_type = "__Type"
    return _resolve(root["selections"], desc, meta_type)


def _resolve(entries: List[tuple], desc: dict, meta_type: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for entry in entries:
        kind = entry[0]
        key = entry[1]
        if kind == "typename":
            data[key] = meta_type
            continue
        _kind, _key, attr, shape, child_meta, child_entries = entry
        value = desc.get(attr)
        if shape == "leaf":
            data[key] = value
        elif shape == "strlist":
            data[key] = list(value) if value is not None else None
        elif shape == "object":
            data[key] = (
                None
                if value is None
                else _resolve(child_entries, value, child_meta)
            )
        else:  # list of meta objects
            data[key] = (
                None
                if value is None
                else [_resolve(child_entries, item, child_meta) for item in value]
            )
    return data
