"""Subscription push: match entity change events against a subscription.

Compiles a subscription operation into an equality filter plus a nested
@link-backed projection tree over a single @entity root, then applies it to an
NDJSON stream of entity change events. GraphQL, variable and @entity mapping
semantics are shared with the query planner; root arguments form equality
filters on the subscription's root entity, while object fields declared with
@link resolve against the freshest target snapshots seen so far in the stream.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .errors import PlanError
from .gql import parse_executable
from .planner import Planner
from .schema import Entity, TypeInfo, named_of

EVENT_OPS = ("INSERT", "UPDATE", "DELETE")


class _Leaf:
    """A selected scalar/enum leaf field."""

    __slots__ = ("response_key", "field_name")

    def __init__(self, response_key: str, field_name: str):
        self.response_key = response_key
        self.field_name = field_name


class _Link:
    """A validated single-object @link at an object field in the selection."""

    __slots__ = (
        "response_key",
        "field_name",
        "local",
        "target",
        "as_array",
        "non_null",
        "target_table",
        "children",
    )

    def __init__(
        self,
        response_key: str,
        field_name: str,
        local: List[str],
        target: List[str],
        as_array: bool,
        non_null: bool,
        target_table: str,
        children: List[Any],
    ):
        self.response_key = response_key
        self.field_name = field_name
        self.local = local  # local field names on the parent entity, declared order
        self.target = target  # target primary-key field names, declared order
        self.as_array = as_array  # whether local/target were declared in array form
        self.non_null = non_null  # whether the object field is non-nullable
        self.target_table = target_table
        self.children = children  # nested _Leaf / _Link nodes, in selection order


class SubscriptionPlan:
    """A compiled subscription ready to filter and project event snapshots."""

    __slots__ = (
        "operation_name",
        "path",
        "entity_table",
        "filter",
        "tree",
        "entity_keys",
    )

    def __init__(
        self,
        operation_name: Optional[str],
        path: str,
        entity_table: str,
        filter_obj: Dict[str, Any],
        tree: List[Any],
        entity_keys: Dict[str, List[str]],
    ):
        self.operation_name = operation_name
        self.path = path
        self.entity_table = entity_table
        self.filter = filter_obj  # entity field name -> expected value
        self.tree = tree  # root selection nodes (_Leaf / _Link), selection order
        # Every entity participating in the projection (root plus each @link
        # target), mapped to its declared primary-key field names in order.
        self.entity_keys = entity_keys

    def matches(self, snapshot: Dict[str, Any]) -> bool:
        for name, expected in self.filter.items():
            if name not in snapshot:
                raise PlanError(
                    "EventError", f"snapshot is missing filter field '{name}'"
                )
            if snapshot[name] != expected:
                return False
        return True


def _is_list_type(type_ref) -> bool:
    """Whether the field's GraphQL type contains a list wrapper."""
    while type_ref[0] in ("list", "non_null"):
        if type_ref[0] == "list":
            return True
        type_ref = type_ref[1]
    return False


def _is_keyable(value: Any) -> bool:
    """Whether a snapshot value can participate in a primary-key tuple."""
    return value is not None and not isinstance(value, (dict, list))


class SubscriptionCompiler(Planner):
    """Reuses the planner's fragment expansion and variable resolution."""

    def compile(
        self, subscription_text: str, source: str, operation_name: Optional[str]
    ) -> SubscriptionPlan:
        operations, self.fragments = parse_executable(subscription_text, source)
        op = self._select_operation(operations, operation_name)
        self.var_map = {}
        for name, type_ref, default, has_default in op.var_defs:
            if name in self.var_map:
                raise PlanError(
                    "VariablesError", f"variable '${name}' is declared more than once"
                )
            self.var_map[name] = (type_ref, default, has_default)

        root_type_name = self.schema.roots.get("subscription")
        root_type = self.schema.types.get(root_type_name) if root_type_name else None
        if root_type is None:
            raise PlanError(
                "MappingError",
                "schema does not define a root type for subscription operations",
            )

        selections = self._expand(op.selection_set, root_type.name, [])
        if not selections:
            raise PlanError("InvalidQuery", "subscription has an empty selection set")
        if len(selections) != 1:
            raise PlanError(
                "InvalidQuery", "subscription must select exactly one root field"
            )
        node = selections[0]
        key = node.alias or node.name

        field = root_type.fields.get(node.name)
        if field is None:
            raise PlanError(
                "InvalidQuery",
                f"unknown root field '{node.name}' on type '{root_type.name}'",
            )
        type_name = named_of(field.type_ref)
        target = self.schema.types.get(type_name)
        entity = target.entity if target is not None else None
        if entity is None:
            raise PlanError(
                "MappingError",
                f"root field '{node.name}' does not return a mapped entity type",
            )
        if node.selection_set is None:
            raise PlanError(
                "InvalidQuery", f"root field '{node.name}' requires a selection set"
            )

        filter_obj = self._compile_filter(field, target, entity.table, node)
        entity_keys: Dict[str, List[str]] = {entity.table: list(entity.key)}
        tree = self._compile_tree(
            target, entity, node.selection_set, key, entity_keys
        )
        return SubscriptionPlan(
            op.name, key, entity.table, filter_obj, tree, entity_keys
        )

    # -- operation selection ---------------------------------------------------

    @staticmethod
    def _select_operation(operations, operation_name):
        if operation_name is not None:
            for op in operations:
                if op.name == operation_name and op.op_type == "subscription":
                    return op
            raise PlanError(
                "InvalidQuery", f"no subscription named '{operation_name}'"
            )
        if not operations:
            raise PlanError("InvalidQuery", "document contains no operations")
        if len(operations) != 1:
            raise PlanError(
                "InvalidQuery",
                "multiple operations found; select one with --operation",
            )
        op = operations[0]
        if op.op_type != "subscription":
            raise PlanError("InvalidQuery", "operation must be a subscription")
        return op

    # -- filter ------------------------------------------------------------------

    def _compile_filter(self, field, target, table, node) -> Dict[str, Any]:
        for arg_name, arg_def in field.args.items():
            if (
                arg_def.type[0] == "non_null"
                and not arg_def.has_default
                and arg_name not in node.args
            ):
                raise PlanError(
                    "InvalidQuery",
                    f"missing required argument '{arg_name}' on root field '{node.name}'",
                )
        filter_obj: Dict[str, Any] = {}
        for arg_name, value in node.args.items():
            if arg_name not in field.args:
                raise PlanError(
                    "InvalidQuery",
                    f"unknown argument '{arg_name}' on root field '{node.name}'",
                )
            target_field = target.fields.get(arg_name)
            if target_field is None or not self.schema.is_leaf(
                named_of(target_field.type_ref)
            ):
                raise PlanError(
                    "InvalidQuery",
                    f"argument '{arg_name}' does not match a filterable field of entity '{table}'",
                )
            filter_obj[arg_name] = self._resolve(value)
        return filter_obj

    # -- nested projection --------------------------------------------------------

    def _compile_tree(
        self,
        info: TypeInfo,
        entity: Entity,
        selection_set,
        path: str,
        entity_keys: Dict[str, List[str]],
    ) -> List[Any]:
        selections = self._expand(selection_set, info.name, [])
        if not selections:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        nodes: List[Any] = []
        seen = set()
        for node in selections:
            key = node.alias or node.name
            node_path = f"{path}.{key}"
            field = info.fields.get(node.name)
            if field is None:
                raise PlanError(
                    "InvalidQuery",
                    f"unknown field '{node.name}' on type '{info.name}'",
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
                    nodes.append(_Leaf(key, node.name))
                continue
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
            if _is_list_type(field.type_ref):
                raise PlanError(
                    "UnsupportedSelection",
                    f"list relation '{node.name}' is not supported in subscriptions",
                )
            target = self.schema.types.get(type_name)
            if target is None:
                raise PlanError(
                    "MappingError",
                    f"field '{info.name}.{node.name}' has unsupported type '{type_name}'",
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
            entity_keys.setdefault(
                target_entity.table, list(target_entity.key)
            )
            children = self._compile_tree(
                target, target_entity, node.selection_set, node_path, entity_keys
            )
            if key not in seen:
                seen.add(key)
                nodes.append(
                    _Link(
                        key,
                        node.name,
                        local,
                        target_key,
                        as_array,
                        field.type_ref[0] == "non_null",
                        target_entity.table,
                        children,
                    )
                )
        if not nodes:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        return nodes


def push_events(
    plan: SubscriptionPlan, events_text: str, source: str
) -> List[Dict[str, Any]]:
    """Apply a compiled subscription to an NDJSON stream, in order.

    Each line is parsed and validated before any snapshot state changes. The
    latest snapshot of every entity participating in the projection is kept in
    a primary-keyed store; an emitted root event projects its own after/before
    snapshot and resolves each selected @link from the freshest target
    snapshots available as of the current line.
    """
    outputs: List[Dict[str, Any]] = []
    # table -> primary key tuple -> latest snapshot dict
    stores: Dict[str, Dict[tuple, Dict[str, Any]]] = {
        table: {} for table in plan.entity_keys
    }

    def primary_key(
        table: str, key_fields: List[str], snapshot: Dict[str, Any], where: str
    ) -> tuple:
        values: List[Any] = []
        for name in key_fields:
            if name not in snapshot:
                raise PlanError(
                    "EventError",
                    f"{where}: snapshot of '{table}' is missing key field '{name}'",
                )
            value = snapshot[name]
            if not _is_keyable(value):
                raise PlanError(
                    "EventError",
                    f"{where}: snapshot of '{table}' has an invalid value for key field '{name}'",
                )
            values.append(value)
        return tuple(values)

    def project(
        nodes: List[Any],
        snapshot: Dict[str, Any],
        table: str,
        where: str,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for node in nodes:
            if isinstance(node, _Leaf):
                if node.field_name not in snapshot:
                    raise PlanError(
                        "EventError",
                        f"{where}: snapshot of '{table}' is missing field '{node.field_name}'",
                    )
                data[node.response_key] = snapshot[node.field_name]
                continue

            local_values: List[Any] = []
            for local_name in node.local:
                if local_name not in snapshot:
                    raise PlanError(
                        "EventError",
                        f"{where}: snapshot of '{table}' is missing @link local field '{local_name}' for '{node.field_name}'",
                    )
            raw_values = [snapshot[name] for name in node.local]
            if any(value is None for value in raw_values):
                if node.non_null:
                    raise PlanError(
                        "EventError",
                        f"{where}: relation '{node.field_name}' on '{table}' resolved to null but is declared non-null",
                    )
                data[node.response_key] = None
                continue
            for value in raw_values:
                if not _is_keyable(value):
                    raise PlanError(
                        "EventError",
                        f"{where}: @link '{node.field_name}' on '{table}' has a value that cannot form the primary key of '{node.target_table}'",
                    )
                local_values.append(value)
            target_snapshot = stores[node.target_table].get(tuple(local_values))
            if target_snapshot is None:
                raise PlanError(
                    "EventError",
                    f"{where}: @link '{node.field_name}' on '{table}' has no current snapshot of '{node.target_table}' with key {tuple(local_values)!r}",
                )
            data[node.response_key] = project(
                node.children, target_snapshot, node.target_table, where
            )
        return data

    for lineno, raw in enumerate(events_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        where = f"{source}:{lineno}"
        event = _parse_event(line, source, lineno)
        table = event["entity"]
        key_fields = plan.entity_keys.get(table)
        if key_fields is None:
            # Events of entities that take no part in this subscription are
            # validated for shape but otherwise skipped.
            continue

        op = event["op"]
        if op in ("INSERT", "UPDATE"):
            snapshot = event["after"]
            if snapshot is None:
                raise PlanError(
                    "EventError", f"{where}: {op} event has no snapshot"
                )
            pk = primary_key(table, key_fields, snapshot, where)
            stores[table][pk] = snapshot
        else:
            snapshot = event["before"]
            if snapshot is None:
                raise PlanError(
                    "EventError", f"{where}: {op} event has no snapshot"
                )
            pk = primary_key(table, key_fields, snapshot, where)
            stores[table].pop(pk, None)

        if table == plan.entity_table and plan.matches(snapshot):
            outputs.append(
                {
                    "subscription": plan.operation_name,
                    "path": plan.path,
                    "event": op,
                    "entity": table,
                    "data": project(plan.tree, snapshot, table, where),
                }
            )
    return outputs


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
    if not isinstance(event["entity"], str):
        raise PlanError(
            "EventError", f"{source}:{lineno}: 'entity' must be a string"
        )
    for key in ("before", "after"):
        if event[key] is not None and not isinstance(event[key], dict):
            raise PlanError(
                "EventError", f"{source}:{lineno}: '{key}' must be an object or null"
            )
    return event
