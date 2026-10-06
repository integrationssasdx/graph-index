"""Query execution: run a GraphQL query over entity change events.

Compiles a query operation into one executable root per root field (equality
filter plus a projection tree, sharing the subscription compiler's selection
and @link semantics), applies an NDJSON event stream to build the latest
snapshot of every reachable entity keyed by primary key, then evaluates the
query once against that final state and returns a GraphQL response object of
the form ``{"data": {...}}``.

Only query operations are supported; mutations and subscriptions end with
UnsupportedOperation. A single-entity root yields the unique matching
snapshot (null when there is none, QueryError when several match); a list
root yields every matching snapshot in first-insert order (UPDATE replaces
in place, DELETE followed by re-INSERT moves the row to the end).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .errors import PlanError
from .gql import parse_executable
from .schema import named_of
from .subscription import (
    LinkedField,
    SubscriptionCompiler,
    _is_list_type,
    _OrderedStore,
    _key_tuple,
    _parse_event,
    _resolve_list,
    _resolve_object,
)


class RootPlan:
    """A compiled root field: filter plus projection over one entity table."""

    __slots__ = ("response_key", "is_list", "entity_table", "filter", "children")

    def __init__(
        self,
        response_key: str,
        is_list: bool,
        entity_table: str,
        filter_obj: Dict[str, Any],
        children: List[LinkedField],
    ):
        self.response_key = response_key
        self.is_list = is_list
        self.entity_table = entity_table
        self.filter = filter_obj  # entity field name -> expected value
        self.children = children


class QueryExecPlan:
    """A compiled query ready to execute against a final snapshot state."""

    __slots__ = ("operation_name", "roots", "entity_keys")

    def __init__(
        self,
        operation_name: Optional[str],
        roots: List[RootPlan],
        entity_keys: Dict[str, List[str]],
    ):
        self.operation_name = operation_name
        self.roots = roots
        # entity table -> primary key field names, in declared order, for
        # every entity reachable from the selected root fields
        self.entity_keys = entity_keys


class QueryCompiler(SubscriptionCompiler):
    """Reuses the subscription compiler's selection tree and @link checks."""

    def compile(
        self, query_text: str, source: str, operation_name: Optional[str]
    ) -> QueryExecPlan:
        operations, self.fragments = parse_executable(query_text, source)
        op = self._select_operation(operations, operation_name)
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
                "UnknownField",
                "schema does not define a root type for query operations",
            )

        selections = self._expand(op.selection_set, root_type.name, [])
        if not selections:
            raise PlanError("InvalidQuery", "operation has an empty selection set")

        entity_keys: Dict[str, List[str]] = {}
        roots = [
            self._compile_root(node, root_type, entity_keys) for node in selections
        ]
        return QueryExecPlan(op.name, roots, entity_keys)

    # -- operation selection ---------------------------------------------------

    @staticmethod
    def _select_operation(operations, operation_name):
        if operation_name is not None:
            for op in operations:
                if op.name == operation_name:
                    if op.op_type != "query":
                        raise PlanError(
                            "UnsupportedOperation",
                            f"{op.op_type} operations are not supported",
                        )
                    return op
            raise PlanError(
                "InvalidOperation", f"no operation named '{operation_name}'"
            )
        if not operations:
            raise PlanError("InvalidRequest", "document contains no operations")
        if len(operations) != 1:
            raise PlanError(
                "InvalidRequest",
                "multiple operations found; select one with --operation",
            )
        op = operations[0]
        if op.op_type != "query":
            raise PlanError(
                "UnsupportedOperation",
                f"{op.op_type} operations are not supported",
            )
        return op

    # -- roots ------------------------------------------------------------------

    def _compile_root(self, node, root_type, entity_keys) -> RootPlan:
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
        entity_keys[entity.table] = list(entity.key)
        children = self._compile_fields(
            target, entity, node.selection_set, key, entity_keys
        )
        return RootPlan(
            key, _is_list_type(field.type_ref), entity.table, filter_obj, children
        )


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------


def execute_events(
    plan: QueryExecPlan, events_text: str, source: str
) -> Dict[str, Any]:
    """Apply an NDJSON event stream, then evaluate the compiled query once.

    Every line is parsed and validated first; the latest snapshot of each
    reachable entity is then maintained by primary key (INSERT/UPDATE store
    ``after``, DELETE removes the ``before`` key), and the query runs against
    the resulting final state.
    """
    events: List = []
    for lineno, raw in enumerate(events_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        events.append((lineno, _parse_event(line, source, lineno)))

    snapshots: Dict[str, _OrderedStore] = {}
    last_lineno = 0
    for lineno, event in events:
        _apply_event(plan.entity_keys, snapshots, event, source, lineno)
        last_lineno = lineno

    data: Dict[str, Any] = {}
    for root in plan.roots:
        data[root.response_key] = _resolve_root(
            root, snapshots, plan.entity_keys, source, last_lineno
        )
    return {"data": data}


def _apply_event(
    entity_keys: Dict[str, List[str]],
    snapshots: Dict[str, _OrderedStore],
    event: Dict[str, Any],
    source: str,
    lineno: int,
) -> None:
    """Update the latest-snapshot store for one validated event.

    Events for entities the query cannot reach are ignored. For reachable
    entities the snapshot that identifies the row (``after`` for
    INSERT/UPDATE, ``before`` for DELETE) must be present and must supply a
    usable scalar primary key; anything else is an EventError.
    """
    key_fields = entity_keys.get(event["entity"])
    if key_fields is None:
        return
    store = snapshots.setdefault(event["entity"], _OrderedStore())
    op = event["op"]
    snapshot = event["after"] if op in ("INSERT", "UPDATE") else event["before"]
    if snapshot is None:
        raise PlanError(
            "EventError", f"{source}:{lineno}: {op} event has no snapshot"
        )
    key = _key_tuple(key_fields, snapshot)
    if key is None:
        raise PlanError(
            "EventError",
            f"{source}:{lineno}: snapshot of entity '{event['entity']}' "
            f"cannot form its primary key",
        )
    if op in ("INSERT", "UPDATE"):
        store.upsert(key, snapshot)
    else:
        store.delete(key)


def _resolve_root(
    root: RootPlan,
    snapshots: Dict[str, _OrderedStore],
    entity_keys: Dict[str, List[str]],
    source: str,
    lineno: int,
) -> Any:
    """Evaluate one root field against the final snapshot state."""
    store = snapshots.get(root.entity_table)
    rows = store.values() if store is not None else []
    matched = [row for row in rows if _matches(root, row, source, lineno)]
    if root.is_list:
        return [
            _project(
                root.children, row, snapshots, entity_keys, source, lineno,
                root.response_key,
            )
            for row in matched
        ]
    if not matched:
        return None
    if len(matched) > 1:
        raise PlanError(
            "QueryError",
            f"root field '{root.response_key}' matched {len(matched)} entities "
            f"of '{root.entity_table}'",
        )
    return _project(
        root.children, matched[0], snapshots, entity_keys, source, lineno,
        root.response_key,
    )


def _matches(
    root: RootPlan, snapshot: Dict[str, Any], source: str, lineno: int
) -> bool:
    for name, expected in root.filter.items():
        if name not in snapshot:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: snapshot is missing filter field '{name}'",
            )
        if snapshot[name] != expected:
            return False
    return True


def _project(
    nodes: List[LinkedField],
    snapshot: Dict[str, Any],
    snapshots: Dict[str, _OrderedStore],
    entity_keys: Dict[str, List[str]],
    source: str,
    lineno: int,
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
                    f"{source}:{lineno}: snapshot is missing field "
                    f"'{node.field_name}'",
                )
            value = snapshot[node.field_name]
            _check_shape(node.type_ref, value, source, lineno, node_path)
            data[node.response_key] = value
            continue
        if node.is_list:
            matches = _resolve_list(
                node, snapshot, snapshots, entity_keys, source, lineno, node_path
            )
            data[node.response_key] = [
                _project(
                    node.children, related, snapshots, entity_keys, source,
                    lineno, node_path,
                )
                for related in matches
            ]
            continue
        related = _resolve_object(node, snapshot, snapshots, source, lineno, node_path)
        if related is None:
            if node.required:
                raise PlanError(
                    "EventError",
                    f"{source}:{lineno}: non-null relationship '{node_path}' "
                    f"resolved to null",
                )
            data[node.response_key] = None
        else:
            data[node.response_key] = _project(
                node.children, related, snapshots, entity_keys, source, lineno,
                node_path,
            )
    return data


def _check_shape(type_ref, value: Any, source: str, lineno: int, path: str) -> None:
    """Ensure a snapshot value matches the declared shape of its field.

    Nullability, list-ness and scalar-ness must line up with the schema; a
    value that does not fit the declared shape is an EventError.
    """
    if type_ref[0] == "non_null":
        if value is None:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: value for '{path}' must not be null",
            )
        _check_shape(type_ref[1], value, source, lineno, path)
        return
    if value is None:
        return
    if type_ref[0] == "list":
        if not isinstance(value, list):
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: value for '{path}' must be a list",
            )
        for item in value:
            _check_shape(type_ref[1], item, source, lineno, path)
        return
    if isinstance(value, (dict, list)):
        raise PlanError(
            "EventError",
            f"{source}:{lineno}: value for '{path}' must be a scalar",
        )
