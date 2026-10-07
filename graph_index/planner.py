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

# Field arguments accepted as list-size bounds while query complexity control
# is enabled, in priority order. Only present when a bound limit is configured,
# so baseline argument validation is unchanged when the control is off.
BOUND_ARG_NAMES = ("first", "limit")

# Stable pagination/sorting arguments accepted on a schema list root field
# that explicitly declares them.
PAGING_ARG_NAMES = ("page", "pageSize", "orderBy", "sortDirection")
SORT_DIRECTIONS = ("ASC", "DESC")


def _is_list_type(type_ref) -> bool:
    """Whether a (possibly non-null wrapped) field type is a list."""
    if type_ref[0] == "non_null":
        type_ref = type_ref[1]
    return type_ref[0] == "list"


class Planner:
    def __init__(self, schema: Schema, variables: Dict[str, Any],
                 bound_args_allowed: bool = False):
        self.schema = schema
        self.variables = variables
        # When a complexity limit is configured, list fields accept first/limit
        # bounds (literal or variable); otherwise arguments stay unsupported.
        self.bound_args_allowed = bound_args_allowed
        self.fragments: Dict[str, Any] = {}
        self.var_map: Dict[str, tuple] = {}
        self.joins: List[dict] = []

    # -- complexity bound arguments ------------------------------------------

    def _accepts_bound_arg(self, is_list_field: bool, arg_name: str) -> bool:
        """Whether a first/limit argument is accepted on this field.

        Bound arguments are only recognized while complexity control is
        enabled and only on list-returning fields; everywhere else the field's
        ordinary argument validation (unknown argument / arguments not
        supported) keeps applying, so baseline behavior is unchanged.
        """
        return (
            self.bound_args_allowed
            and is_list_field
            and arg_name in BOUND_ARG_NAMES
        )

    # -- stable pagination / sorting -----------------------------------------

    def _compile_paging(
        self, node: FieldNode, field, target: TypeInfo
    ) -> Dict[str, Any]:
        """Validate and resolve the four paging arguments of a list root.

        The arguments are only recognized when the schema field itself
        declares them; otherwise they keep failing as unknown arguments in the
        caller. Static literals that violate the contract fail with
        InvalidQuery; variable-provided failures surface as VariablesError.
        """
        def declared(arg_name: str) -> bool:
            return arg_name in field.args

        page_present = declared("page") and "page" in node.args
        size_present = declared("pageSize") and "pageSize" in node.args
        # An explicit null counts as "not supplied" for orderBy/sortDirection:
        # both arguments are nullable strings, and null leaves insertion order.
        order_present = (
            declared("orderBy")
            and node.args.get("orderBy") is not None
        )
        direction_present = (
            declared("sortDirection")
            and node.args.get("sortDirection") is not None
        )

        page = 0
        if page_present:
            page = self._resolve_paging_int("page", node.args["page"], True)
        page_size = None
        if size_present:
            page_size = self._resolve_paging_int(
                "pageSize", node.args["pageSize"], False
            )
        order_by = None
        if order_present:
            order_by = self._resolve_order_by(node.args["orderBy"], target)
        sort_direction = "ASC"
        if direction_present:
            sort_direction = self._resolve_sort_direction(
                node.args["sortDirection"], node.name
            )

        if page_present and not size_present:
            raise PlanError(
                "InvalidQuery",
                f"argument 'page' on root field '{node.name}' requires 'pageSize'",
            )
        if direction_present and not order_present:
            raise PlanError(
                "InvalidQuery",
                f"argument 'sortDirection' on root field '{node.name}' "
                f"requires 'orderBy'",
            )
        return {
            "page": page,
            "pageSize": page_size,
            "orderBy": order_by,
            "sortDirection": sort_direction,
        }

    def _resolve_paging_int(
        self, arg_name: str, value: Any, allow_zero: bool
    ) -> int:
        """Resolve page/pageSize, validating literal/variable integer values.

        Static literals fail with InvalidQuery; variable-provided values fail
        with VariablesError. A nullable variable resolving to null is likewise
        a VariablesError: paging positions require an actual integer.
        """
        from_var = isinstance(value, Var)
        if from_var:
            resolved = self._resolve(value)
        else:
            resolved = value
        if isinstance(resolved, bool) or not isinstance(resolved, int):
            raise PlanError(
                "VariablesError" if from_var else "InvalidQuery",
                f"argument '{arg_name}' must be an integer",
            )
        if resolved < 0 or (not allow_zero and resolved == 0):
            raise PlanError(
                "VariablesError" if from_var else "InvalidQuery",
                f"argument '{arg_name}' has an invalid value {resolved}",
            )
        return resolved

    def _resolve_order_by(self, value: Any, target: TypeInfo) -> str:
        """Resolve orderBy to a sortable, non-null root-entity field name."""
        from_var = isinstance(value, Var)
        if from_var:
            resolved = self._resolve(value)
        else:
            resolved = value
        # EnumLiteral is a str subclass, so an unquoted field name behaves the
        # same as the quoted string it resolves to in filter positions.
        if not isinstance(resolved, str):
            raise PlanError(
                "VariablesError" if from_var else "InvalidQuery",
                "argument 'orderBy' must be a field name string",
            )
        self._check_order_field(resolved, target, from_var)
        return str(resolved)

    def _check_order_field(self, name: str, target: TypeInfo, from_var: bool) -> None:
        """Reject orderBy targets that are unknown, nullable, list or composite."""
        code = "VariablesError" if from_var else "InvalidQuery"
        order_field = target.fields.get(name)
        if order_field is None:
            raise PlanError(
                code,
                f"orderBy field '{name}' is not a field of type '{target.name}'",
            )
        if _is_list_type(order_field.type_ref):
            raise PlanError(
                code, f"orderBy field '{name}' must not be a list field"
            )
        if order_field.type_ref[0] != "non_null":
            raise PlanError(code, f"orderBy field '{name}' must be non-null")
        named = named_of(order_field.type_ref)
        if (
            named not in ("Int", "Float", "String", "ID", "Boolean")
            and named not in self.schema.enums
        ):
            raise PlanError(
                code,
                f"orderBy field '{name}' must be a non-list scalar or enum field",
            )

    def _resolve_sort_direction(self, value: Any, field_name: str) -> str:
        from_var = isinstance(value, Var)
        if from_var:
            resolved = self._resolve(value)
        else:
            resolved = value
        if not isinstance(resolved, str) or str(resolved) not in SORT_DIRECTIONS:
            raise PlanError(
                "VariablesError" if from_var else "InvalidQuery",
                f"argument 'sortDirection' on root field '{field_name}' must "
                f"be ASC or DESC",
            )
        return str(resolved)

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
        is_list = _is_list_type(field.type_ref)
        paging = self._compile_paging(node, field, target) if is_list else None
        filter_obj: Dict[str, Any] = {}
        for arg_name, value in node.args.items():
            if paging is not None and arg_name in PAGING_ARG_NAMES:
                if arg_name in field.args:
                    continue
                raise PlanError(
                    "UnknownField",
                    f"unknown argument '{arg_name}' on root field '{node.name}'",
                )
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
        entry = {
            "path": key,
            "entity": entity.table,
            "filter": filter_obj,
            "fields": [],
            "page": paging["page"] if paging else 0,
            "pageSize": paging["pageSize"] if paging else None,
            "orderBy": paging["orderBy"] if paging else None,
            "sortDirection": paging["sortDirection"] if paging else "ASC",
        }
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
