"""Query plan builder.

Turns an executable GraphQL document plus resolved variables into a scan/join
plan over the entities declared in the schema. All validation failures raise
PlanError with the appropriate code; no partial plan is produced.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import PlanError
from .gql import (
    FieldNode,
    FragmentSpread,
    InlineFragment,
    Var,
    parse_executable,
)
from .schema import Schema, TypeInfo, named_of


class Planner:
    def __init__(self, schema: Schema, variables: Dict[str, Any]):
        self.schema = schema
        self.variables = variables
        self.fragments: Dict[str, Any] = {}
        self.var_map: Dict[str, tuple] = {}
        self.joins: List[dict] = []

    def complexity_of(self, schema: Schema, op, variables: Dict[str, Any]) -> int:
        """Measure an operation reusing this compiler's fragment/var state.

        Imported lazily so the query-planner path does not depend on the
        complexity module when the feature is disabled.
        """
        from .complexity import measure_complexity

        return measure_complexity(
            schema, op, self.fragments, variables, self.var_map, self._check_type
        )

    # -- entry point ---------------------------------------------------------

    def plan(self, query_text: str, source: str, operation_name: Optional[str]) -> dict:
        operations, self.fragments = parse_executable(query_text, source)
        op = self._select_operation(operations, operation_name)
        self.var_map = {}
        for name, type_ref, default, has_default in op.var_defs:
            if name in self.var_map:
                raise PlanError("VariablesError", f"variable '${name}' is declared more than once")
            self.var_map[name] = (type_ref, default, has_default)

        root_type_name = self.schema.roots.get(op.op_type)
        root_type = self.schema.types.get(root_type_name) if root_type_name else None
        if root_type is None:
            raise PlanError(
                "UnknownField",
                f"schema does not define a root type for {op.op_type} operations",
            )

        selections = self._expand(op.selection_set, root_type.name, [])
        if not selections:
            raise PlanError("InvalidQuery", "operation has an empty selection set")

        plan = {
            "operationType": op.op_type,
            "operationName": op.name,
            "roots": [],
            "joins": self.joins,
        }
        for node in selections:
            plan["roots"].append(self._plan_root(node, root_type))
        return plan

    # -- operation selection ---------------------------------------------------

    @staticmethod
    def _select_operation(operations, operation_name):
        if operation_name is not None:
            for op in operations:
                if op.name == operation_name:
                    if op.op_type == "subscription":
                        raise PlanError(
                            "UnsupportedOperation",
                            "subscription operations are not supported",
                        )
                    return op
            raise PlanError("InvalidOperation", f"no operation named '{operation_name}'")
        candidates = [op for op in operations if op.op_type in ("query", "mutation")]
        if len(candidates) == 1:
            return candidates[0]
        if not operations:
            raise PlanError("InvalidRequest", "document contains no operations")
        if not candidates and len(operations) == 1:
            raise PlanError(
                "UnsupportedOperation", "subscription operations are not supported"
            )
        raise PlanError(
            "InvalidRequest",
            "multiple operations found; select one with --operation",
        )

    # -- fragment expansion ------------------------------------------------------

    def _type_condition_matches(self, condition: str, type_name: str) -> bool:
        if condition == type_name:
            return True
        info = self.schema.types.get(type_name)
        if info is not None and condition in info.interfaces:
            return True
        members = self.schema.unions.get(condition)
        return bool(members) and type_name in members

    def _expand(self, selections, type_name: str, stack: List[str]) -> List[FieldNode]:
        """Inline fragments and merge fields by response key, preserving order."""
        out: List[FieldNode] = []
        index: Dict[str, FieldNode] = {}

        def add(node: FieldNode) -> None:
            key = node.alias or node.name
            existing = index.get(key)
            if existing is None:
                index[key] = FieldNode(
                    node.name,
                    node.alias,
                    dict(node.args),
                    list(node.selection_set) if node.selection_set is not None else None,
                )
                out.append(index[key])
                return
            if existing.name != node.name:
                raise PlanError(
                    "InvalidQuery",
                    f"conflicting fields '{existing.name}' and '{node.name}' share response key '{key}'",
                )
            if (existing.selection_set is None) != (node.selection_set is None):
                raise PlanError(
                    "InvalidQuery", f"conflicting selections for response key '{key}'"
                )
            if existing.selection_set is not None:
                existing.selection_set.extend(node.selection_set)

        for sel in selections:
            if isinstance(sel, FieldNode):
                add(sel)
            elif isinstance(sel, FragmentSpread):
                frag = self.fragments.get(sel.name)
                if frag is None:
                    raise PlanError("InvalidQuery", f"unknown fragment '{sel.name}'")
                if sel.name in stack:
                    raise PlanError(
                        "InvalidQuery", f"fragment cycle involving '{sel.name}'"
                    )
                if self._type_condition_matches(frag.type_condition, type_name):
                    for node in self._expand(frag.selection_set, type_name, stack + [sel.name]):
                        add(node)
            elif isinstance(sel, InlineFragment):
                if sel.type_condition is None or self._type_condition_matches(
                    sel.type_condition, type_name
                ):
                    for node in self._expand(sel.selection_set, type_name, stack):
                        add(node)
        return out

    # -- variables ------------------------------------------------------------

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, Var):
            name = value.name
            if name not in self.var_map:
                raise PlanError(
                    "VariablesError", f"variable '${name}' is not declared by the operation"
                )
            type_ref, default, has_default = self.var_map[name]
            if name in self.variables:
                provided = self.variables[name]
                if not self._check_type(type_ref, provided):
                    raise PlanError(
                        "VariablesError",
                        f"variable '${name}' has a value of the wrong type",
                    )
                return provided
            if has_default:
                return default
            raise PlanError("VariablesError", f"variable '${name}' was not provided")
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        return value

    def _check_type(self, type_ref, value) -> bool:
        kind = type_ref[0]
        if kind == "non_null":
            return value is not None and self._check_type(type_ref[1], value)
        if value is None:
            return True
        if kind == "list":
            return isinstance(value, list) and all(
                self._check_type(type_ref[1], item) for item in value
            )
        name = type_ref[1]
        if name == "Int":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "Float":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if name == "String":
            return isinstance(value, str)
        if name == "Boolean":
            return isinstance(value, bool)
        if name == "ID":
            return isinstance(value, (str, int)) and not isinstance(value, bool)
        if name in self.schema.scalars:
            return True
        if name in self.schema.enums:
            return isinstance(value, str)
        input_def = self.schema.inputs.get(name)
        if input_def is not None:
            if not isinstance(value, dict):
                return False
            for field_name, field_def in input_def.items():
                if (
                    field_def.type[0] == "non_null"
                    and not field_def.has_default
                    and field_name not in value
                ):
                    return False
                if field_name in value and not self._check_type(
                    field_def.type, value[field_name]
                ):
                    return False
            return True
        return True  # unknown named type: accept

    # -- roots ------------------------------------------------------------------

    def _plan_root(self, node: FieldNode, root_type: TypeInfo) -> dict:
        key = node.alias or node.name
        field = root_type.fields.get(node.name)
        if field is None:
            raise PlanError(
                "UnknownField", f"unknown root field '{node.name}' on type '{root_type.name}'"
            )
        type_name = named_of(field.type_ref)
        target = self.schema.types.get(type_name)
        entity = target.entity if target is not None else None
        if entity is None:
            raise PlanError(
                "UnknownEntity",
                f"root field '{node.name}' does not return a mapped entity type",
            )
        if node.selection_set is None:
            raise PlanError(
                "InvalidQuery", f"root field '{node.name}' requires a selection set"
            )
        for arg_name, arg_def in field.args.items():
            if (
                arg_def.type[0] == "non_null"
                and not arg_def.has_default
                and arg_name not in node.args
            ):
                raise PlanError(
                    "UnknownField",
                    f"missing required argument '{arg_name}' on root field '{node.name}'",
                )
        filter_obj: Dict[str, Any] = {}
        for arg_name, value in node.args.items():
            if arg_name not in field.args:
                raise PlanError(
                    "UnknownField",
                    f"unknown argument '{arg_name}' on root field '{node.name}'",
                )
            target_field = target.fields.get(arg_name)
            if target_field is None or not self.schema.is_leaf(
                named_of(target_field.type_ref)
            ):
                raise PlanError(
                    "UnknownField",
                    f"argument '{arg_name}' does not match a filterable field of entity '{entity.table}'",
                )
            filter_obj[arg_name] = self._resolve(value)
        entry = {"path": key, "entity": entity.table, "filter": filter_obj, "fields": []}
        self._walk(target, entity, node.selection_set, key, entry)
        return entry

    # -- nested selections --------------------------------------------------------

    def _walk(self, info: TypeInfo, entity, selection_set, path: str, entry: dict) -> None:
        selections = self._expand(selection_set, info.name, [])
        if not selections:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        for node in selections:
            key = node.alias or node.name
            node_path = f"{path}.{key}"
            field = info.fields.get(node.name)
            if field is None:
                raise PlanError(
                    "UnknownField", f"unknown field '{node.name}' on type '{info.name}'"
                )
            type_name = named_of(field.type_ref)
            if self.schema.is_leaf(type_name):
                if node.selection_set is not None:
                    raise PlanError(
                        "InvalidQuery",
                        f"scalar field '{node.name}' must not have a selection set",
                    )
                if node.args:
                    raise PlanError(
                        "InvalidQuery",
                        f"arguments on field '{node.name}' are not supported",
                    )
                if node_path not in entry["fields"]:
                    entry["fields"].append(node_path)
                continue
            target = self.schema.types.get(type_name)
            if target is None:
                raise PlanError(
                    "UnknownField",
                    f"field '{node.name}' has unsupported type '{type_name}'",
                )
            if node.args:
                raise PlanError(
                    "InvalidQuery",
                    f"arguments on field '{node.name}' are not supported",
                )
            if node.selection_set is None:
                raise PlanError(
                    "InvalidQuery", f"field '{node.name}' requires a selection set"
                )
            if field.link is None:
                raise PlanError(
                    "MappingError",
                    f"field '{info.name}.{node.name}' is missing an @link directive",
                )
            target_entity = target.entity
            if target_entity is None:
                raise PlanError(
                    "UnknownEntity",
                    f"type '{target.name}' is not mapped to an entity",
                )
            local, target_key, as_array = field.link
            if target_key != target_entity.key:
                raise PlanError(
                    "InvalidJoin",
                    f"@link target {target_key} does not match the primary key of entity '{target_entity.table}'",
                )
            self.joins.append(
                {
                    "path": node_path,
                    "fromEntity": entity.table,
                    "fromField": local if as_array else local[0],
                    "toEntity": target_entity.table,
                    "toField": target_key if as_array else target_key[0],
                }
            )
            self._walk(target, target_entity, node.selection_set, node_path, entry)
