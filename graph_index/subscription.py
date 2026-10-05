"""Subscription push: match entity change events against a subscription.

Compiles a subscription operation into an equality filter plus a field
projection over a single @entity root, then applies it to an NDJSON stream of
entity change events. GraphQL, variable and @entity mapping semantics are
shared with the query planner; the supported selection shape covers leaf
fields, nested object fields and list relationship fields reached through
@link joins (one or more levels deep, including aliases and fragment
spreads). List relationships match every current target snapshot whose
@link target values equal the source's local values; unlike single-object
links, their target fields need not form the target primary key.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .errors import PlanError
from .gql import parse_executable
from .planner import Planner
from .schema import Entity, TypeInfo, named_of

EVENT_OPS = ("INSERT", "UPDATE", "DELETE")

_MISSING = object()


class SubscriptionPlan:
    """A compiled subscription ready to filter and project event snapshots."""

    __slots__ = (
        "operation_name",
        "path",
        "entity_table",
        "entity_keys",
        "filter",
        "tree",
    )

    def __init__(
        self,
        operation_name: Optional[str],
        path: str,
        entity_table: str,
        entity_keys: Dict[str, List[str]],
        filter_obj: Dict[str, Any],
        tree: "LinkedField",
    ):
        self.operation_name = operation_name
        self.path = path
        self.entity_table = entity_table
        # entity table -> primary key field names, in declared order, for the
        # root entity and every entity reachable through selected @link fields
        self.entity_keys = entity_keys
        self.filter = filter_obj  # entity field name -> expected value
        self.tree = tree  # compiled selection tree rooted at the root field

    def matches(self, snapshot: Dict[str, Any]) -> bool:
        for name, expected in self.filter.items():
            if name not in snapshot:
                raise PlanError(
                    "EventError", f"snapshot is missing filter field '{name}'"
                )
            if snapshot[name] != expected:
                return False
        return True


class LinkedField:
    """A compiled field in the subscription selection tree.

    Leaf fields carry ``field_name`` and no children. Link fields carry the
    validated @link mapping (local/target field names, target entity) and a
    list of compiled children; ``is_list`` marks fields whose type is a list
    of objects, which match every current target snapshot by equality and
    project to an array.
    """

    __slots__ = (
        "response_key",
        "field_name",
        "required",
        "local",
        "target",
        "target_table",
        "is_list",
        "children",
    )

    def __init__(
        self,
        response_key: str,
        field_name: str,
        required: bool = False,
        local: Optional[List[str]] = None,
        target: Optional[List[str]] = None,
        target_table: Optional[str] = None,
        is_list: bool = False,
        children: Optional[List["LinkedField"]] = None,
    ):
        self.response_key = response_key
        self.field_name = field_name
        self.required = required  # field declared as a non-null object type
        self.local = local  # source-side key field names, in @link order
        self.target = target  # target-side field names, in @link order
        self.target_table = target_table
        self.is_list = is_list  # field type is a list of objects
        self.children = children


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
        children = self._compile_fields(
            target, entity, node.selection_set, key, entity_keys
        )
        tree = LinkedField(key, key, children=children)
        return SubscriptionPlan(
            op.name, key, entity.table, entity_keys, filter_obj, tree
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

    # -- selection tree -----------------------------------------------------------

    def _compile_fields(
        self,
        info: TypeInfo,
        entity: Entity,
        selection_set,
        path: str,
        entity_keys: Dict[str, List[str]],
    ) -> List[LinkedField]:
        selections = self._expand(selection_set, info.name, [])
        if not selections:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        children: List[LinkedField] = []
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
                    children.append(LinkedField(key, node.name))
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
            is_list = _is_list_type(field.type_ref)
            required = not is_list and field.type_ref[0] == "non_null"
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
            local, target_key, _as_array = field.link
            if not is_list and target_key != target_entity.key:
                raise PlanError(
                    "InvalidJoin",
                    f"@link target {target_key} does not match the primary key of "
                    f"entity '{target_entity.table}'",
                )
            nested = self._compile_fields(
                target_info,
                target_entity,
                node.selection_set,
                node_path,
                entity_keys,
            )
            if key not in seen:
                seen.add(key)
                entity_keys[target_entity.table] = list(target_entity.key)
                children.append(
                    LinkedField(
                        key,
                        node.name,
                        required=required,
                        local=list(local),
                        target=list(target_key),
                        target_table=target_entity.table,
                        is_list=is_list,
                        children=nested,
                    )
                )
        return children


def _is_list_type(type_ref) -> bool:
    """Whether a (possibly non-null wrapped) field type is a list."""
    if type_ref[0] == "non_null":
        type_ref = type_ref[1]
    return type_ref[0] == "list"


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------


def push_events(
    plan: SubscriptionPlan, events_text: str, source: str
) -> List[Dict[str, Any]]:
    """Apply a compiled subscription to an NDJSON event stream, in order.

    Every line is parsed and validated first; the latest snapshot of each
    entity is then maintained by primary key, and matching root events are
    projected against the snapshots known up to and including that line.
    """
    events: List[Tuple[int, Dict[str, Any]]] = []
    for lineno, raw in enumerate(events_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        events.append((lineno, _parse_event(line, source, lineno)))

    snapshots: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]] = {}
    outputs: List[Dict[str, Any]] = []
    for lineno, event in events:
        _apply_event(plan.entity_keys, snapshots, event)
        if event["entity"] != plan.entity_table:
            continue
        op = event["op"]
        snapshot = event["after"] if op in ("INSERT", "UPDATE") else event["before"]
        if snapshot is None:
            raise PlanError(
                "EventError", f"{source}:{lineno}: {op} event has no snapshot"
            )
        if not plan.matches(snapshot):
            continue
        data = _project(
            plan.tree.children, snapshot, snapshots, source, lineno, plan.path
        )
        outputs.append(
            {
                "subscription": plan.operation_name,
                "path": plan.path,
                "event": op,
                "entity": event["entity"],
                "data": data,
            }
        )
    return outputs


def _apply_event(
    entity_keys: Dict[str, List[str]],
    snapshots: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]],
    event: Dict[str, Any],
) -> None:
    """Update the latest-snapshot store for one validated event.

    Events for entities the subscription cannot reach are ignored. Rows whose
    snapshot lacks a usable primary key cannot identify an entity; they are
    skipped here and surface as an EventError when a matching root event tries
    to traverse the relationship, rather than at their own line.
    """
    key_fields = entity_keys.get(event["entity"])
    if key_fields is None:
        return
    store = snapshots.setdefault(event["entity"], {})
    if event["op"] in ("INSERT", "UPDATE"):
        snapshot = event["after"]
        key = _key_tuple(key_fields, snapshot)
        if snapshot is not None and key is not None:
            store[key] = snapshot
        return
    snapshot = event["before"]
    key = _key_tuple(key_fields, snapshot)
    if snapshot is not None and key is not None:
        store.pop(key, None)


def _key_tuple(
    key_fields: List[str], snapshot: Optional[Dict[str, Any]]
) -> Optional[Tuple[Any, ...]]:
    """Build a primary-key tuple, or None when the snapshot cannot supply one."""
    if not isinstance(snapshot, dict):
        return None
    values: List[Any] = []
    for name in key_fields:
        value = snapshot.get(name)
        if value is None or isinstance(value, (dict, list)):
            return None
        values.append(value)
    return tuple(values)


def _project(
    nodes: List[LinkedField],
    snapshot: Dict[str, Any],
    snapshots: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]],
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
            data[node.response_key] = snapshot[node.field_name]
            continue
        if node.is_list:
            matched = _resolve_list_link(
                node, snapshot, snapshots, source, lineno, node_path
            )
            data[node.response_key] = [
                _project(node.children, item, snapshots, source, lineno, node_path)
                for item in matched
            ]
            continue
        related = _resolve_link(node, snapshot, snapshots, source, lineno, node_path)
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
                node.children, related, snapshots, source, lineno, node_path
            )
    return data


def _resolve_link(
    node: LinkedField,
    snapshot: Dict[str, Any],
    snapshots: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]],
    source: str,
    lineno: int,
    path: str,
) -> Optional[Dict[str, Any]]:
    """Follow one @link from a snapshot to the latest target snapshot.

    Returns None when any local key value is null (the relationship itself is
    absent). Missing local key fields, values that cannot form the target
    primary key or a target key without a current snapshot end the stream with
    EventError.
    """
    local_values: List[Any] = []
    for name in node.local:
        if name not in snapshot:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: snapshot is missing local key field "
                f"'{name}' for '{path}'",
            )
        value = snapshot[name]
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: local key field '{name}' for '{path}' "
                f"cannot form a primary key",
            )
        local_values.append(value)

    target_store = snapshots.get(node.target_table, {})
    related = target_store.get(tuple(local_values), _MISSING)
    if related is _MISSING:
        rendered = ", ".join(
            f"{name}: {json.dumps(value)}"
            for name, value in zip(node.target, local_values)
        )
        raise PlanError(
            "EventError",
            f"{source}:{lineno}: no current snapshot of entity "
            f"'{node.target_table}' for '{path}' with key {{{rendered}}}",
        )
    for name in node.target:
        if name not in related:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: target snapshot is missing key field "
                f"'{name}' for '{path}'",
            )
    return related


def _resolve_list_link(
    node: LinkedField,
    snapshot: Dict[str, Any],
    snapshots: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]],
    source: str,
    lineno: int,
    path: str,
) -> List[Dict[str, Any]]:
    """Collect every current target snapshot matching one list @link.

    Matching is equality between the source's local values and each target
    snapshot's target values; the target fields need not be the target
    primary key. Returns matches in the order the targets first entered the
    snapshot store (an UPDATE keeps its position, a re-INSERT after DELETE
    sorts last). Any null local value means the empty list. Missing local
    key fields, local values that cannot form a scalar key and target
    snapshots missing a target field end the stream with EventError.
    """
    local_values: List[Any] = []
    for name in node.local:
        if name not in snapshot:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: snapshot is missing local key field "
                f"'{name}' for '{path}'",
            )
        value = snapshot[name]
        if value is None:
            return []
        if isinstance(value, (dict, list)):
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: local key field '{name}' for '{path}' "
                f"cannot form a scalar key",
            )
        local_values.append(value)

    matched: List[Dict[str, Any]] = []
    target_store = snapshots.get(node.target_table, {})
    for related in target_store.values():
        target_values: List[Any] = []
        for name in node.target:
            if name not in related:
                raise PlanError(
                    "EventError",
                    f"{source}:{lineno}: target snapshot is missing target field "
                    f"'{name}' for '{path}'",
                )
            target_values.append(related[name])
        if target_values == local_values:
            matched.append(related)
    return matched


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
