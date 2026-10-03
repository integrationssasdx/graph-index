"""Schema model: object types plus the @entity / @link mapping directives.

Mapping declaration problems (missing or conflicting entity/directive
declarations, dangling type references) raise PlanError "MappingError".
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .errors import PlanError
from .gql import Directive, TypeRef, parse_schema_document

BUILTIN_SCALARS = frozenset({"Int", "Float", "String", "Boolean", "ID"})


def named_of(type_ref: TypeRef) -> str:
    """Unwrap list/non-null wrappers down to the named type."""
    while type_ref[0] in ("list", "non_null"):
        type_ref = type_ref[1]
    return type_ref[1]


class Entity:
    __slots__ = ("type_name", "table", "key")

    def __init__(self, type_name: str, table: str, key: List[str]):
        self.type_name = type_name
        self.table = table
        self.key = key  # primary key field names, in declared order


class FieldInfo:
    __slots__ = ("name", "type_ref", "args", "link_directives", "link")

    def __init__(self, name, type_ref, args, link_directives):
        self.name = name
        self.type_ref = type_ref
        self.args = args  # Dict[str, InputValueDef]
        self.link_directives = link_directives
        # (local names, target names, declared_as_arrays) once validated
        self.link: Optional[Tuple[List[str], List[str], bool]] = None


class TypeInfo:
    __slots__ = ("name", "fields", "interfaces", "entity")

    def __init__(self, name: str, interfaces: List[str]):
        self.name = name
        self.fields: Dict[str, FieldInfo] = {}
        self.interfaces = interfaces
        self.entity: Optional[Entity] = None


class Schema:
    def __init__(self):
        self.types: Dict[str, TypeInfo] = {}
        self.scalars = set(BUILTIN_SCALARS)
        self.enums = set()
        self.inputs: Dict[str, Dict[str, object]] = {}
        self.interfaces = set()
        self.unions: Dict[str, List[str]] = {}
        self.roots: Dict[str, str] = {}  # "query" | "mutation" | "subscription" -> type name

    def is_leaf(self, type_name: str) -> bool:
        return type_name in self.scalars or type_name in self.enums

    def known_type(self, type_name: str) -> bool:
        return (
            type_name in self.scalars
            or type_name in self.enums
            or type_name in self.types
            or type_name in self.inputs
            or type_name in self.interfaces
            or type_name in self.unions
        )


def _mapping_error(message: str) -> PlanError:
    return PlanError("MappingError", message)


def load_schema(text: str, source: str = "<schema>") -> Schema:
    doc = parse_schema_document(text, source)
    schema = Schema()
    schema.scalars |= set(doc.scalars)
    schema.enums = set(doc.enums)
    schema.interfaces = {tdef.name for tdef in doc.interfaces}
    schema.unions = dict(doc.unions)

    for input_def in doc.inputs:
        if input_def.name in schema.inputs:
            raise _mapping_error(f"duplicate input type '{input_def.name}'")
        fields: Dict[str, object] = {}
        for fdef in input_def.fields:
            if fdef.name in fields:
                raise _mapping_error(
                    f"duplicate field '{fdef.name}' on input '{input_def.name}'"
                )
            fields[fdef.name] = fdef
        schema.inputs[input_def.name] = fields

    # Merge plain definitions with `extend type` blocks.
    slots: Dict[str, dict] = {}
    for tdef in doc.types:
        slot = slots.setdefault(
            tdef.name, {"fields": [], "directives": [], "interfaces": [], "defined": False}
        )
        if not tdef.is_extend:
            if slot["defined"]:
                raise _mapping_error(f"duplicate definition of type '{tdef.name}'")
            slot["defined"] = True
        slot["fields"].extend(tdef.fields)
        slot["directives"].extend(tdef.directives)
        for iface in tdef.interfaces:
            if iface not in slot["interfaces"]:
                slot["interfaces"].append(iface)

    reserved = set(schema.scalars) | schema.enums | set(schema.inputs) | schema.interfaces | set(schema.unions)
    for name in slots:
        if name in reserved:
            raise _mapping_error(f"name '{name}' is defined more than once")

    for name, slot in slots.items():
        info = TypeInfo(name, slot["interfaces"])
        for iface in slot["interfaces"]:
            if iface not in schema.interfaces:
                raise _mapping_error(f"type '{name}' implements unknown interface '{iface}'")
        for fdef in slot["fields"]:
            if fdef.name in info.fields:
                raise _mapping_error(f"duplicate field '{fdef.name}' on type '{name}'")
            args: Dict[str, object] = {}
            for adef in fdef.args:
                if adef.name in args:
                    raise _mapping_error(
                        f"duplicate argument '{adef.name}' on '{name}.{fdef.name}'"
                    )
                args[adef.name] = adef
            link_directives = [d for d in fdef.directives if d.name == "link"]
            info.fields[fdef.name] = FieldInfo(fdef.name, fdef.type, args, link_directives)
        entity_directives = [d for d in slot["directives"] if d.name == "entity"]
        if len(entity_directives) > 1:
            raise _mapping_error(f"type '{name}' has multiple @entity directives")
        if entity_directives:
            info.entity = _build_entity(schema, info, entity_directives[0])
        schema.types[name] = info

    _validate_type_references(schema)
    _validate_links(schema)
    _validate_entity_tables(schema)
    _resolve_roots(schema, doc.roots)
    return schema


def _normalize_field_list(value, what: str) -> Tuple[List[str], bool]:
    """Normalize a string-or-string-array directive argument.

    Returns (field names in declared order, declared_as_array). Anything
    else (empty arrays, non-string elements, duplicates, other types) is a
    MappingError.
    """
    if isinstance(value, str):
        if not value:
            raise _mapping_error(f"{what} requires a non-empty string")
        return [value], False
    if isinstance(value, list):
        if not value:
            raise _mapping_error(f"{what} must not be an empty array")
        names: List[str] = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise _mapping_error(f"{what} must contain only non-empty strings")
            if item in names:
                raise _mapping_error(f"{what} contains duplicate field '{item}'")
            names.append(item)
        return names, True
    raise _mapping_error(f"{what} must be a string or an array of strings")


def _build_entity(schema: Schema, info: TypeInfo, directive: Directive) -> Entity:
    table = directive.args.get("name")
    if not isinstance(table, str) or not table:
        raise _mapping_error(f"@entity on '{info.name}' requires a non-empty string 'name'")
    key, _ = _normalize_field_list(
        directive.args.get("key"), f"@entity 'key' of type '{info.name}'"
    )
    for name in key:
        key_field = info.fields.get(name)
        if key_field is None:
            raise _mapping_error(f"@entity key '{name}' is not a field of type '{info.name}'")
        if named_of(key_field.type_ref) not in schema.scalars:
            raise _mapping_error(f"@entity key '{name}' of type '{info.name}' must be a scalar")
    return Entity(info.name, table, key)


def _validate_type_references(schema: Schema) -> None:
    def check(type_ref: TypeRef, where: str) -> None:
        name = named_of(type_ref)
        if not schema.known_type(name):
            raise _mapping_error(f"unknown type '{name}' referenced by {where}")

    for info in schema.types.values():
        for field in info.fields.values():
            check(field.type_ref, f"'{info.name}.{field.name}'")
            for arg in field.args.values():
                check(arg.type, f"argument '{arg.name}' of '{info.name}.{field.name}'")
    for input_name, fields in schema.inputs.items():
        for fdef in fields.values():
            check(fdef.type, f"input field '{input_name}.{fdef.name}'")
    for union_name, members in schema.unions.items():
        for member in members:
            if member not in schema.types:
                raise _mapping_error(f"union '{union_name}' references unknown type '{member}'")


def _validate_links(schema: Schema) -> None:
    for info in schema.types.values():
        for field in info.fields.values():
            directives = field.link_directives
            if len(directives) > 1:
                raise _mapping_error(f"field '{info.name}.{field.name}' has multiple @link directives")
            if not directives:
                continue
            directive = directives[0]
            where = f"@link on '{info.name}.{field.name}'"
            local, local_is_array = _normalize_field_list(
                directive.args.get("local"), f"{where} argument 'local'"
            )
            target, target_is_array = _normalize_field_list(
                directive.args.get("target"), f"{where} argument 'target'"
            )
            if local_is_array != target_is_array:
                raise _mapping_error(
                    f"{where} must use the same form (string or array) for 'local' and 'target'"
                )
            if len(local) != len(target):
                raise _mapping_error(
                    f"{where} has {len(local)} local field(s) but {len(target)} target field(s)"
                )
            target_type = named_of(field.type_ref)
            target_info = schema.types.get(target_type)
            if target_info is None:
                raise _mapping_error(
                    f"@link on '{info.name}.{field.name}' must point to an object type"
                )
            for name in local:
                local_field = info.fields.get(name)
                if local_field is None:
                    raise _mapping_error(
                        f"@link local field '{name}' is not a field of type '{info.name}'"
                    )
                if named_of(local_field.type_ref) not in schema.scalars:
                    raise _mapping_error(
                        f"@link local field '{name}' of type '{info.name}' must be a scalar"
                    )
            for name in target:
                if name not in target_info.fields:
                    raise _mapping_error(
                        f"@link target field '{name}' is not a field of type '{target_type}'"
                    )
            field.link = (local, target, local_is_array)


def _validate_entity_tables(schema: Schema) -> None:
    tables: Dict[str, str] = {}
    for info in schema.types.values():
        if info.entity is None:
            continue
        other = tables.get(info.entity.table)
        if other is not None:
            raise _mapping_error(
                f"entity '{info.entity.table}' is mapped by both '{other}' and '{info.name}'"
            )
        tables[info.entity.table] = info.name


def _resolve_roots(schema: Schema, declared: Dict[str, str]) -> None:
    for op, default_name in (("query", "Query"), ("mutation", "Mutation"), ("subscription", "Subscription")):
        if default_name in schema.types:
            schema.roots[op] = default_name
    for op, type_name in declared.items():
        if type_name not in schema.types:
            raise _mapping_error(f"root type '{type_name}' for {op} is not defined")
        schema.roots[op] = type_name
