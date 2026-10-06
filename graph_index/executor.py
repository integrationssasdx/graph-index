"""Direct GraphQL query execution over entity change events.

Compiles a query operation into per-root filters and selection trees (sharing
the planner's fragment, variable and @link semantics), folds an NDJSON entity
change stream into the current primary-key snapshot of every reachable
entity, then answers the query once against those snapshots.

List roots return every matching row in first-insert order (UPDATE replaces a
row in place; DELETE followed by re-INSERT moves it to the end); single-object
roots return null on no match and fail with QueryError on more than one match.
Single-object @link fields follow local -> target primary key; list @link
fields equality-match arbitrary scalar target fields in target insert order.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .errors import PlanError
from .gql import Var, parse_executable
from .planner import Planner
from .schema import Entity, Schema, TypeInfo, named_of
from .subscription import EVENT_OPS, _OrderedStore, _is_list_type

_MISSING = object()


class ExecNode:
    """A compiled field in the query selection tree.

    Leaf fields carry ``field_type`` and no children. Link fields also carry
    the validated @link mapping (local/target field names, target entity) and
    their compiled children; ``is_list`` selects list resolution.
    """

    __slots__ = (
        "response_key",
        "field_name",
        "field_type",
        "required",
        "is_list",
        "local",
        "local_types",
        "target",
        "target_types",
        "target_table",
        "children",
    )

    def __init__(
        self,
        response_key: str,
        field_name: str,
        field_type=None,
        required: bool = False,
        is_list: bool = False,
        local: Optional[List[str]] = None,
        local_types: Optional[List[Any]] = None,
        target: Optional[List[str]] = None,
        target_types: Optional[List[Any]] = None,
        target_table: Optional[str] = None,
        children: Optional[List["ExecNode"]] = None,
    ):
        self.response_key = response_key
        self.field_name = field_name
        self.field_type = field_type
        self.required = required
        self.is_list = is_list
        self.local = local
        self.local_types = local_types
        self.target = target
        self.target_types = target_types
        self.target_table = target_table
        self.children = children


class ExecContext:
    """Everything execution needs beyond the compiled roots."""

    __slots__ = ("schema", "entity_keys", "key_types", "snapshots")

    def __init__(self, schema: Schema, entity_keys: Dict[str, List[str]],
                 key_types: Dict[str, List[Any]]):
        self.schema = schema
        self.entity_keys = entity_keys
        # table -> primary key field type refs, in declared order
        self.key_types = key_types
        self.snapshots: Dict[str, _OrderedStore] = {}


class QueryCompiler(Planner):
    """Compiles a query document into one executable entry per root field."""

    def __init__(self, schema: Schema, variables: Dict[str, Any],
                 bound_args_allowed: bool = False):
        super().__init__(schema, variables, bound_args_allowed)
        self.entity_keys: Dict[str, List[str]] = {}
        self.key_types: Dict[str, List[Any]] = {}

    def compile(
        self, query_text: str, source: str, operation_name: Optional[str]
    ) -> Tuple[Optional[str], List[dict]]:
        operations, self.fragments = parse_executable(query_text, source)
        op = self._select_operation(operations, operation_name)
        if op.op_type != "query":
            raise PlanError(
                "UnsupportedOperation",
                f"{op.op_type} operations are not supported by query-exec",
            )
        self.var_map = {}
        for name, type_ref, default, has_default in op.var_defs:
            if name in self.var_map:
                raise PlanError(
                    "VariablesError", f"variable '${name}' is declared more than once"
                )
            self.var_map[name] = (type_ref, default, has_default)

        root_type_name = self.schema.roots.get("query")
        root_type = self.schema.types.get(root_type_name) if root_type_name else None
        if root_type is None:
            raise PlanError(
                "MappingError",
                "schema does not define a root type for query operations",
            )

        selections = self._expand(op.selection_set, root_type.name, [])
        if not selections:
            raise PlanError("InvalidQuery", "query has an empty selection set")

        roots: List[dict] = []
        self.entity_keys = {}
        self.key_types = {}
        for node in selections:
            # Introspection root fields cost 0 and are only recognized while
            # complexity control is enabled; the engine models no introspection
            # data, so such a root resolves deterministically to null.
            if self.bound_args_allowed and node.name in ("__schema", "__type"):
                roots.append({"introspection": node.alias or node.name})
                continue
            roots.append(self._compile_root(node, root_type))
        return op.name, roots

    # -- operation selection ---------------------------------------------------

    @staticmethod
    def _select_operation(operations, operation_name):
        if operation_name is not None:
            for op in operations:
                if op.name == operation_name:
                    return op
            raise PlanError("InvalidOperation", f"no operation named '{operation_name}'")
        if not operations:
            raise PlanError("InvalidRequest", "document contains no operations")
        if len(operations) == 1:
            return operations[0]
        raise PlanError(
            "InvalidRequest",
            "multiple operations found; select one with --operation",
        )

    # -- roots ------------------------------------------------------------------

    def _compile_root(self, node, root_type: TypeInfo) -> dict:
        key = node.alias or node.name
        field = root_type.fields.get(node.name)
        if field is None:
            raise PlanError(
                "UnknownField",
                f"unknown root field '{node.name}' on type '{root_type.name}'",
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
        filter_obj: Dict[str, Any] = {}
        filter_types: Dict[str, Any] = {}
        for arg_name, value in node.args.items():
            if self._accepts_bound_arg(is_list, arg_name):
                # A list-size bound used for complexity; never a filter. If it
                # is a variable, resolve it so type/required-variable errors
                # are reported by the normal validation phase.
                if isinstance(value, Var):
                    self._resolve(value)
                continue
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
                    f"argument '{arg_name}' does not match a filterable field of "
                    f"entity '{entity.table}'",
                )
            filter_obj[arg_name] = self._resolve(value)
            filter_types[arg_name] = target_field.type_ref

        self.entity_keys[entity.table] = list(entity.key)
        self.key_types[entity.table] = [
            target.fields[name].type_ref for name in entity.key
        ]
        children = self._compile_fields(
            target, entity, node.selection_set, key
        )
        return {
            "response_key": key,
            "field_name": node.name,
            "table": entity.table,
            "filter": filter_obj,
            "filter_types": filter_types,
            "is_list": is_list,
            "children": children,
        }

    # -- selection tree -----------------------------------------------------------

    def _compile_fields(
        self,
        info: TypeInfo,
        entity: Entity,
        selection_set,
        path: str,
    ) -> List[ExecNode]:
        selections = self._expand(selection_set, info.name, [])
        if not selections:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        children: List[ExecNode] = []
        seen = set()
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
                if key not in seen:
                    seen.add(key)
                    children.append(ExecNode(key, node.name, field.type_ref))
                continue
            is_list = _is_list_type(field.type_ref)
            for arg_name, value in node.args.items():
                if self._accepts_bound_arg(is_list, arg_name):
                    if isinstance(value, Var):
                        self._resolve(value)
                    continue
                raise PlanError(
                    "InvalidQuery",
                    f"arguments on field '{node.name}' are not supported",
                )
            if node.selection_set is None:
                raise PlanError(
                    "InvalidQuery", f"field '{node.name}' requires a selection set"
                )
            required = field.type_ref[0] == "non_null"
            if field.link is None:
                raise PlanError(
                    "MappingError",
                    f"field '{info.name}.{node.name}' is missing an @link directive",
                )
            target_info = self.schema.types.get(type_name)
            if target_info is None:
                raise PlanError(
                    "MappingError",
                    f"field '{info.name}.{node.name}' has unsupported type '{type_name}'",
                )
            target_entity = target_info.entity
            if target_entity is None:
                raise PlanError(
                    "UnknownEntity",
                    f"type '{target_info.name}' is not mapped to an entity",
                )
            local, target_fields, _as_array = field.link
            if is_list:
                for name in local:
                    if not _is_scalar_value_field(self.schema, info.fields.get(name)):
                        raise PlanError(
                            "MappingError",
                            f"@link local field '{name}' of type '{info.name}' "
                            f"must be a scalar",
                        )
                for name in target_fields:
                    matched = target_info.fields.get(name)
                    if matched is None:
                        raise PlanError(
                            "MappingError",
                            f"@link target field '{name}' is not a field of type "
                            f"'{target_info.name}'",
                        )
                    if not _is_scalar_value_field(self.schema, matched):
                        raise PlanError(
                            "MappingError",
                            f"@link target field '{name}' of type "
                            f"'{target_info.name}' must be a scalar",
                        )
            elif target_fields != target_entity.key:
                raise PlanError(
                    "InvalidJoin",
                    f"@link target {target_fields} does not match the primary key of "
                    f"entity '{target_entity.table}'",
                )
            nested = self._compile_fields(
                target_info, target_entity, node.selection_set, node_path
            )
            if key not in seen:
                seen.add(key)
                self.entity_keys[target_entity.table] = list(target_entity.key)
                self.key_types[target_entity.table] = [
                    target_info.fields[name].type_ref for name in target_entity.key
                ]
                children.append(
                    ExecNode(
                        key,
                        node.name,
                        field.type_ref,
                        required=required,
                        is_list=is_list,
                        local=list(local),
                        local_types=[info.fields[n].type_ref for n in local],
                        target=list(target_fields),
                        target_types=[
                            target_info.fields[n].type_ref for n in target_fields
                        ],
                        target_table=target_entity.table,
                        children=nested,
                    )
                )
        return children


def _is_scalar_value_field(schema: Schema, field) -> bool:
    """Whether a field holds a single (non-list) scalar value."""
    if field is None:
        return False
    return (
        not _is_list_type(field.type_ref)
        and named_of(field.type_ref) in schema.scalars
    )


# ---------------------------------------------------------------------------
# Event folding
# ---------------------------------------------------------------------------


def fold_events(ctx: ExecContext, events_text: str, source: str) -> None:
    """Parse and validate every event line, then fold them into snapshots."""
    events: List[Tuple[int, Dict[str, Any]]] = []
    for lineno, raw in enumerate(events_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        events.append((lineno, _parse_event(line, source, lineno)))

    for lineno, event in events:
        _apply_event(ctx, event, source, lineno)


def _parse_event(line: str, source: str, lineno: int) -> Dict[str, Any]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PlanError("EventError", f"{source}:{lineno}: invalid JSON: {exc.msg}")
    if not isinstance(event, dict):
        raise PlanError("EventError", f"{source}:{lineno}: event must be a JSON object")
    for key in ("op", "entity", "before", "after"):
        if key not in event:
            raise PlanError(
                "EventError", f"{source}:{lineno}: event is missing '{key}'"
            )
    if event["op"] not in EVENT_OPS:
        raise PlanError(
            "EventError", f"{source}:{lineno}: unknown op {event['op']!r}"
        )
    if not isinstance(event["entity"], str) or not event["entity"]:
        raise PlanError(
            "EventError", f"{source}:{lineno}: 'entity' must be a non-empty string"
        )
    for key in ("before", "after"):
        if event[key] is not None and not isinstance(event[key], dict):
            raise PlanError(
                "EventError", f"{source}:{lineno}: '{key}' must be an object or null"
            )
    return event


def _apply_event(
    ctx: ExecContext, event: Dict[str, Any], source: str, lineno: int
) -> None:
    """Maintain the current snapshot store for one validated event.

    Events for entities the query cannot reach are ignored (as in
    subscription-push); for every reachable entity the operative snapshot
    must carry a well-formed scalar primary key.
    """
    key_fields = ctx.entity_keys.get(event["entity"])
    if key_fields is None:
        return
    op = event["op"]
    if op in ("INSERT", "UPDATE"):
        snapshot = event["after"]
        if snapshot is None:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: {op} event requires an 'after' snapshot",
            )
        key = _require_key(
            key_fields, ctx.key_types[event["entity"]], snapshot,
            event["entity"], ctx.schema, source, lineno,
        )
        ctx.snapshots.setdefault(event["entity"], _OrderedStore()).upsert(key, snapshot)
        return
    snapshot = event["before"]
    if snapshot is None:
        raise PlanError(
            "EventError",
            f"{source}:{lineno}: DELETE event requires a 'before' snapshot",
        )
    key = _require_key(
        key_fields, ctx.key_types[event["entity"]], snapshot,
        event["entity"], ctx.schema, source, lineno,
    )
    ctx.snapshots.setdefault(event["entity"], _OrderedStore()).delete(key)


def _require_key(
    key_fields: List[str],
    key_types: List[Any],
    snapshot: Dict[str, Any],
    table: str,
    schema: Schema,
    source: str,
    lineno: int,
) -> Tuple[Any, ...]:
    """Build a primary-key tuple, rejecting any malformed or incomplete one."""
    values: List[Any] = []
    for name, type_ref in zip(key_fields, key_types):
        if name not in snapshot:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: snapshot of entity '{table}' is missing "
                f"primary key field '{name}'",
            )
        value = snapshot[name]
        if value is None or isinstance(value, (dict, list)):
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: primary key field '{name}' of entity '{table}' "
                f"cannot be null or a composite value",
            )
        _check_value_shape(schema, type_ref, value, f"{table}.{name}")
        values.append(value)
    return tuple(values)


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


def execute(roots: List[dict], ctx: ExecContext) -> Dict[str, Any]:
    """Answer the compiled query against the current snapshot stores."""
    data: Dict[str, Any] = {}
    for root in roots:
        if "introspection" in root:
            # No introspection data is modeled; a cost-0 introspection root is
            # answered deterministically with null.
            data[root["introspection"]] = None
            continue
        data[root["response_key"]] = _execute_root(root, ctx)
    return {"data": data}


def _execute_root(root: dict, ctx: ExecContext) -> Any:
    store = ctx.snapshots.get(root["table"])
    rows: List[Dict[str, Any]] = []
    if store is not None:
        for row in store.values():
            if _matches_filter(root, row, ctx):
                rows.append(row)
    path = root["response_key"]
    if root["is_list"]:
        return [_project(root["children"], row, ctx, path) for row in rows]
    if not rows:
        return None
    if len(rows) > 1:
        raise PlanError(
            "QueryError",
            f"single-object root field '{path}' matched {len(rows)} entities",
        )
    return _project(root["children"], rows[0], ctx, path)


def _matches_filter(root: dict, snapshot: Dict[str, Any], ctx: ExecContext) -> bool:
    for name, expected in root["filter"].items():
        if name not in snapshot:
            raise PlanError(
                "EventError",
                f"snapshot of entity '{root['table']}' is missing filter field "
                f"'{name}'",
            )
        value = snapshot[name]
        _check_value_shape(
            ctx.schema,
            root["filter_types"][name],
            value,
            f"{root['response_key']}({name})",
        )
        if value != expected:
            return False
    return True


def _project(
    nodes: List[ExecNode],
    snapshot: Dict[str, Any],
    ctx: ExecContext,
    path: str,
) -> Dict[str, Any]:
    """Project compiled selections over one entity snapshot."""
    data: Dict[str, Any] = {}
    for node in nodes:
        node_path = f"{path}.{node.response_key}"
        if node.children is None:
            if node.field_name not in snapshot:
                raise PlanError(
                    "EventError",
                    f"snapshot is missing field '{node.field_name}' for '{node_path}'",
                )
            value = snapshot[node.field_name]
            _check_value_shape(ctx.schema, node.field_type, value, node_path)
            data[node.response_key] = value
            continue
        if node.is_list:
            data[node.response_key] = _project_list(node, snapshot, ctx, node_path)
            continue
        related = _resolve_object(node, snapshot, ctx, node_path)
        if related is None:
            if node.required:
                raise PlanError(
                    "EventError",
                    f"non-null relationship '{node_path}' resolved to null",
                )
            data[node.response_key] = None
        else:
            data[node.response_key] = _project(
                node.children, related, ctx, node_path
            )
    return data


def _project_list(
    node: ExecNode,
    snapshot: Dict[str, Any],
    ctx: ExecContext,
    path: str,
) -> List[Dict[str, Any]]:
    matches = _resolve_list(node, snapshot, ctx, path)
    return [_project(node.children, related, ctx, path) for related in matches]


# -- @link resolution -----------------------------------------------------------


def _read_local(
    node: ExecNode, snapshot: Dict[str, Any], ctx: ExecContext, path: str
) -> Optional[List[Any]]:
    """Read @link local values; None means any local value is null."""
    local_values: List[Any] = []
    for name, type_ref in zip(node.local, node.local_types):
        if name not in snapshot:
            raise PlanError(
                "EventError",
                f"snapshot is missing local key field '{name}' for '{path}'",
            )
        value = snapshot[name]
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            raise PlanError(
                "EventError",
                f"local key field '{name}' for '{path}' cannot form a scalar key",
            )
        _check_value_shape(ctx.schema, type_ref, value, path)
        local_values.append(value)
    return local_values


def _check_target_fields(
    node: ExecNode, related: Dict[str, Any], ctx: ExecContext, path: str
) -> None:
    """Ensure every matched target field is present with a compatible scalar."""
    for name, type_ref in zip(node.target, node.target_types):
        if name not in related:
            raise PlanError(
                "EventError",
                f"target snapshot of entity '{node.target_table}' is missing key "
                f"field '{name}' for '{path}'",
            )
        value = related[name]
        if isinstance(value, (dict, list)):
            raise PlanError(
                "EventError",
                f"target key field '{name}' for '{path}' cannot be matched as a "
                f"scalar",
            )
        if value is not None:
            _check_value_shape(ctx.schema, type_ref, value, path)


def _check_primary_key(node: ExecNode, ctx: ExecContext, related: Dict[str, Any],
                       path: str) -> None:
    """Ensure a matched target snapshot supplies a well-formed primary key."""
    key_fields = ctx.entity_keys[node.target_table]
    key_types = ctx.key_types[node.target_table]
    for name, type_ref in zip(key_fields, key_types):
        if name not in related:
            raise PlanError(
                "EventError",
                f"target snapshot of entity '{node.target_table}' for '{path}' is "
                f"missing primary key field '{name}'",
            )
        value = related[name]
        if isinstance(value, (dict, list)) or value is None:
            raise PlanError(
                "EventError",
                f"primary key field '{name}' of entity '{node.target_table}' for "
                f"'{path}' cannot be a key",
            )
        _check_value_shape(ctx.schema, type_ref, value, path)


def _resolve_object(
    node: ExecNode,
    snapshot: Dict[str, Any],
    ctx: ExecContext,
    path: str,
) -> Optional[Dict[str, Any]]:
    """Follow a single-object @link to the unique current target snapshot."""
    local_values = _read_local(node, snapshot, ctx, path)
    if local_values is None:
        return None

    target_store = ctx.snapshots.get(node.target_table)
    related = (
        _MISSING
        if target_store is None
        else target_store.rows.get(tuple(local_values), _MISSING)
    )
    if related is _MISSING:
        rendered = ", ".join(
            f"{name}: {json.dumps(value)}"
            for name, value in zip(node.target, local_values)
        )
        raise PlanError(
            "EventError",
            f"no current snapshot of entity '{node.target_table}' for '{path}' "
            f"with key {{{rendered}}}",
        )
    _check_target_fields(node, related, ctx, path)
    return related


def _resolve_list(
    node: ExecNode,
    snapshot: Dict[str, Any],
    ctx: ExecContext,
    path: str,
) -> List[Dict[str, Any]]:
    """Match all current target snapshots for a list @link, in insert order.

    An empty list stands for both an absent relationship (any local value
    null) and one without a current match.
    """
    local_values = _read_local(node, snapshot, ctx, path)
    if local_values is None:
        return []

    target_store = ctx.snapshots.get(node.target_table)
    if target_store is None:
        return []
    expected = tuple(local_values)
    matches: List[Dict[str, Any]] = []
    for related in target_store.values():
        _check_target_fields(node, related, ctx, path)
        values = tuple(related[name] for name in node.target)
        if values != expected or None in values:
            continue
        _check_primary_key(node, ctx, related, path)
        matches.append(related)
    return matches


# ---------------------------------------------------------------------------
# Scalar / enum / list shape validation against the schema
# ---------------------------------------------------------------------------


def _check_value_shape(
    schema: Schema, type_ref, value: Any, path: str
) -> None:
    """Validate a snapshot value against a field's declared shape.

    A null value is accepted for nullable positions and rejected for
    non-null ones; lists must be JSON arrays (elements checked recursively,
    honoring their own non-null wrappers) and leaf values must belong to the
    declared scalar/enum primitive family.
    """
    non_null = type_ref[0] == "non_null"
    if non_null:
        type_ref = type_ref[1]
    if value is None:
        if non_null:
            raise PlanError(
                "EventError", f"non-null field '{path}' resolved to null"
            )
        return
    if type_ref[0] == "list":
        if not isinstance(value, list):
            raise PlanError(
                "EventError",
                f"field '{path}' expects a list but the snapshot value is not an array",
            )
        for item in value:
            _check_value_shape(schema, type_ref[1], item, path)
        return
    name = type_ref[1]
    if isinstance(value, (dict, list)):
        raise PlanError(
            "EventError", f"field '{path}' cannot hold a composite value"
        )
    if name == "Int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise PlanError("EventError", f"field '{path}' expects an Int value")
    elif name == "Float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PlanError("EventError", f"field '{path}' expects a Float value")
    elif name == "String":
        if not isinstance(value, str):
            raise PlanError("EventError", f"field '{path}' expects a String value")
    elif name == "ID":
        if not (
            isinstance(value, str)
            or (isinstance(value, int) and not isinstance(value, bool))
        ):
            raise PlanError("EventError", f"field '{path}' expects an ID value")
    elif name == "Boolean":
        if not isinstance(value, bool):
            raise PlanError("EventError", f"field '{path}' expects a Boolean value")
    elif name in schema.enums:
        if not isinstance(value, str):
            raise PlanError(
                "EventError", f"enum field '{path}' expects a string enum value"
            )
    # Other custom scalar names accept any scalar JSON value.
