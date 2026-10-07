"""Subscription push: match entity change events against a subscription.

Compiles a subscription operation into an equality filter plus a field
projection over a single @entity root, then applies it to an NDJSON stream of
entity change events. GraphQL, variable and @entity mapping semantics are
shared with the query planner; the supported selection shape covers leaf
fields, nested object fields and nested list fields reached through @link
joins (one or more levels deep, including aliases and fragment spreads).

List fields match every current target snapshot whose @link target field
values equal the source's local field values; the matched targets are
recursively projected in target insert order. The @link target of a list
relationship need not be (part of) the target entity's primary key.

Every event line is parsed and validated before the first snapshot changes.
Besides a root entity's own INSERT/UPDATE/DELETE (pushed only while the
snapshot matches the filter), an event on any entity reachable through the
selected @link tree pushes a synthetic UPDATE for each still-matching root
whose projected shape changed when the event was applied.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .errors import PlanError
from .gql import Var, parse_executable
from .planner import Planner, _is_list_type
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
    list of compiled children; ``is_list`` selects list (ordered, possibly
    many) rather than single-object resolution.
    """

    __slots__ = (
        "response_key",
        "field_name",
        "required",
        "is_list",
        "local",
        "target",
        "target_table",
        "children",
    )

    def __init__(
        self,
        response_key: str,
        field_name: str,
        required: bool = False,
        is_list: bool = False,
        local: Optional[List[str]] = None,
        target: Optional[List[str]] = None,
        target_table: Optional[str] = None,
        children: Optional[List["LinkedField"]] = None,
    ):
        self.response_key = response_key
        self.field_name = field_name
        self.required = required  # field declared as a non-null object type
        self.is_list = is_list
        self.local = local  # source-side key field names, in @link order
        self.target = target  # target-side matched field names, in @link order
        self.target_table = target_table
        self.children = children


class SubscriptionCompiler(Planner):
    """Reuses the planner's fragment expansion and variable resolution."""

    def __init__(self, schema, variables, bound_args_allowed: bool = False):
        super().__init__(schema, variables, bound_args_allowed)

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
        is_list = _is_list_type(field.type_ref)
        filter_obj: Dict[str, Any] = {}
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
            local, target_key, _as_array = field.link
            if is_list:
                # A list relationship matches equality on arbitrary scalar
                # target fields (they need not form the target primary key),
                # so every matched field must hold a single scalar value.
                for name in local:
                    matched_local = info.fields.get(name)
                    if not _is_scalar_value_field(self.schema, matched_local):
                        raise PlanError(
                            "MappingError",
                            f"@link local field '{name}' of type '{info.name}' "
                            f"must be a scalar",
                        )
                for name in target_key:
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
            elif target_key != target_entity.key:
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
                        is_list=is_list,
                        local=list(local),
                        target=list(target_key),
                        target_table=target_entity.table,
                        children=nested,
                    )
                )
        return children


def _is_scalar_value_field(schema, field) -> bool:
    """Whether a field holds a single (non-list) built-in/custom scalar."""
    if field is None:
        return False
    return (
        not _is_list_type(field.type_ref)
        and named_of(field.type_ref) in schema.scalars
    )


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------


class _OrderedStore:
    """Latest snapshots of one entity, kept in insert order.

    INSERT introduces a new row at the end; UPDATE replaces a row in place and
    preserves its position; DELETE removes a row, so a later re-INSERT is
    appended at the end again. Snapshots whose primary key cannot be built
    cannot participate in keyed identity (and therefore cannot be deleted by
    key); they are retained separately so a list match on non-key target
    fields can surface them as an EventError instead of silently dropping them.
    """

    __slots__ = ("rows", "keyless")

    def __init__(self):
        self.rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.keyless: List[Dict[str, Any]] = []

    def upsert(self, key: Optional[Tuple[Any, ...]], snapshot: Dict[str, Any]) -> None:
        # Dict assignment appends a new key at the end and replaces an existing
        # key's value without moving it, which is exactly the required order.
        if key is None:
            self.keyless.append(snapshot)
        else:
            self.rows[key] = snapshot

    def delete(self, key: Optional[Tuple[Any, ...]]) -> None:
        # A keyed row is removed so a later upsert re-appends it at the end; a
        # keyless DELETE cannot identify a stored row and leaves them untouched.
        if key is not None:
            self.rows.pop(key, None)

    def values(self) -> List[Dict[str, Any]]:
        return list(self.rows.values())


def push_events(
    plan: SubscriptionPlan, events_text: str, source: str
) -> List[Dict[str, Any]]:
    """Apply a compiled subscription to an NDJSON event stream, in order.

    Every line is parsed and validated first; the latest snapshot of each
    entity is then maintained by primary key, and matching root events are
    projected against the snapshots known up to and including that line.

    An event on an entity reachable through the selected @link tree also
    pushes a synthetic UPDATE for every root that still matches the filter
    after the event and whose projected shape changed; the affected roots are
    reported in their current snapshot insert order.
    """
    events: List[Tuple[int, Dict[str, Any]]] = []
    for lineno, raw in enumerate(events_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        events.append((lineno, _parse_event(line, source, lineno)))

    snapshots: Dict[str, _OrderedStore] = {}
    outputs: List[Dict[str, Any]] = []
    for lineno, event in events:
        # Events on entities the selection tree cannot reach stay read-only:
        # they neither change a store nor get to test a relationship.
        if event["entity"] not in plan.entity_keys:
            continue
        # Project every currently matching root against pre-event snapshots so
        # the post-event comparison can tell an actual projection change from
        # noise. This runs before the store mutates, so a malformed line ends
        # the stream without partial output.
        before_rows = _root_rows(plan, snapshots)
        before_shapes: Dict[Any, Any] = {}
        for identity, snapshot in before_rows:
            if plan.matches(snapshot):
                before_shapes[identity] = _project(
                    plan.tree.children,
                    snapshot,
                    snapshots,
                    plan.entity_keys,
                    source,
                    lineno,
                    plan.path,
                )
        _apply_event(plan.entity_keys, snapshots, event)
        excluded: Optional[set] = None
        if event["entity"] == plan.entity_table:
            direct_identity = _direct_identity(plan, snapshots, event)
            excluded = {direct_identity}
            output = _push_root_event(
                plan, event, snapshots, source, lineno
            )
            if output is not None:
                outputs.append(output)
        outputs.extend(
            _push_dependency_changes(
                plan,
                snapshots,
                source,
                lineno,
                before_shapes,
                excluded,
            )
        )
    return outputs


def _root_rows(
    plan: SubscriptionPlan, snapshots: Dict[str, _OrderedStore]
) -> List[Tuple[Any, Dict[str, Any]]]:
    """Current root snapshots in stable insert order with an identity key.

    Keyed rows use their primary-key tuple; keyless rows use their stored
    object identity so an UPDATE that appends another keyless snapshot is not
    mistaken for the same root.
    """
    store = snapshots.get(plan.entity_table)
    if store is None:
        return []
    key_fields = plan.entity_keys[plan.entity_table]
    rows: List[Tuple[Any, Dict[str, Any]]] = [
        (_key_tuple(key_fields, snapshot), snapshot)
        for snapshot in store.rows.values()
    ]
    rows.extend((id(snapshot), snapshot) for snapshot in store.keyless)
    return rows


def _direct_identity(
    plan: SubscriptionPlan,
    snapshots: Dict[str, _OrderedStore],
    event: Dict[str, Any],
) -> Any:
    """Store identity of the row a root entity event directly targets."""
    op = event["op"]
    snapshot = event["after"] if op in ("INSERT", "UPDATE") else event["before"]
    key = _key_tuple(plan.entity_keys[plan.entity_table], snapshot)
    if key is not None:
        return key
    if op in ("INSERT", "UPDATE"):
        store = snapshots.get(plan.entity_table)
        if store is not None:
            for keyless in store.keyless:
                if keyless is snapshot:
                    return id(keyless)
    return id(snapshot)


def _push_root_event(
    plan: SubscriptionPlan,
    event: Dict[str, Any],
    snapshots: Dict[str, _OrderedStore],
    source: str,
    lineno: int,
) -> Optional[Dict[str, Any]]:
    """Emit the direct record for a root entity INSERT/UPDATE/DELETE.

    Returns the record, or None when the operative snapshot does not match
    the filter. Root events keep their original behavior even when the root
    table is also reachable through a self-referential @link.
    """
    op = event["op"]
    snapshot = event["after"] if op in ("INSERT", "UPDATE") else event["before"]
    if snapshot is None:
        raise PlanError(
            "EventError", f"{source}:{lineno}: {op} event has no snapshot"
        )
    if not plan.matches(snapshot):
        return None
    data = _project(
        plan.tree.children,
        snapshot,
        snapshots,
        plan.entity_keys,
        source,
        lineno,
        plan.path,
    )
    return {
        "subscription": plan.operation_name,
        "path": plan.path,
        "event": op,
        "entity": event["entity"],
        "data": data,
    }


def _push_dependency_changes(
    plan: SubscriptionPlan,
    snapshots: Dict[str, _OrderedStore],
    source: str,
    lineno: int,
    before_shapes: Dict[Any, Any],
    excluded: Optional[set],
) -> List[Dict[str, Any]]:
    """Append synthetic UPDATEs for roots whose projected shape changed.

    Only roots present both before and after the event, still matching the
    filter and not carrying their own direct record for this line qualify;
    they are reported in the current stable root insert order.
    """
    outputs: List[Dict[str, Any]] = []
    for identity, snapshot in _root_rows(plan, snapshots):
        if excluded is not None and identity in excluded:
            continue
        if identity not in before_shapes or not plan.matches(snapshot):
            continue
        data = _project(
            plan.tree.children,
            snapshot,
            snapshots,
            plan.entity_keys,
            source,
            lineno,
            plan.path,
        )
        if before_shapes[identity] == data:
            continue
        outputs.append(
            {
                "subscription": plan.operation_name,
                "path": plan.path,
                "event": "UPDATE",
                "entity": plan.entity_table,
                "data": data,
            }
        )
    return outputs


def _apply_event(
    entity_keys: Dict[str, List[str]],
    snapshots: Dict[str, _OrderedStore],
    event: Dict[str, Any],
) -> None:
    """Update the latest-snapshot store for one validated event.

    Events for entities the subscription cannot reach are ignored. Rows whose
    snapshot lacks a usable primary key cannot identify an entity; they are
    retained separately so a list match on non-key target fields can surface
    them as an EventError instead of silently dropping them.
    """
    key_fields = entity_keys.get(event["entity"])
    if key_fields is None:
        return
    store = snapshots.setdefault(event["entity"], _OrderedStore())
    if event["op"] in ("INSERT", "UPDATE"):
        snapshot = event["after"]
        if snapshot is not None:
            store.upsert(_key_tuple(key_fields, snapshot), snapshot)
        return
    snapshot = event["before"]
    if snapshot is not None:
        store.delete(_key_tuple(key_fields, snapshot))


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
            data[node.response_key] = snapshot[node.field_name]
            continue
        if node.is_list:
            data[node.response_key] = _project_list(
                node, snapshot, snapshots, entity_keys, source, lineno, node_path
            )
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
                node.children,
                related,
                snapshots,
                entity_keys,
                source,
                lineno,
                node_path,
            )
    return data


def _project_list(
    node: LinkedField,
    snapshot: Dict[str, Any],
    snapshots: Dict[str, _OrderedStore],
    entity_keys: Dict[str, List[str]],
    source: str,
    lineno: int,
    path: str,
) -> List[Dict[str, Any]]:
    """Resolve a list @link and project each matched target snapshot."""
    matches = _resolve_list(
        node, snapshot, snapshots, entity_keys, source, lineno, path
    )
    return [
        _project(
            node.children,
            related,
            snapshots,
            entity_keys,
            source,
            lineno,
            path,
        )
        for related in matches
    ]


def _read_local(
    node: LinkedField,
    snapshot: Dict[str, Any],
    source: str,
    lineno: int,
    path: str,
) -> Optional[List[Any]]:
    """Read the @link local key values from a source snapshot.

    Returns None when any local value is null (the relationship itself is
    absent). Missing local key fields or values that cannot form a scalar key
    end the stream with EventError.
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
                f"cannot form a scalar key",
            )
        local_values.append(value)
    return local_values


def _check_target_fields(
    node: LinkedField,
    related: Dict[str, Any],
    source: str,
    lineno: int,
    path: str,
) -> None:
    """Ensure every matched target field is present (and scalar) on a snapshot."""
    for name in node.target:
        if name not in related:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: target snapshot is missing key field "
                f"'{name}' for '{path}'",
            )
        if isinstance(related[name], (dict, list)):
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: target key field '{name}' for '{path}' "
                f"cannot be matched as a scalar",
            )


def _check_primary_key(
    table: str,
    key_fields: List[str],
    related: Dict[str, Any],
    source: str,
    lineno: int,
    path: str,
) -> None:
    """Ensure a matched target snapshot supplies its scalar primary key."""
    for name in key_fields:
        if name not in related:
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: target snapshot of entity '{table}' for "
                f"'{path}' is missing primary key field '{name}'",
            )
        if isinstance(related[name], (dict, list)):
            raise PlanError(
                "EventError",
                f"{source}:{lineno}: primary key field '{name}' of entity "
                f"'{table}' for '{path}' cannot be a key",
            )


def _resolve_object(
    node: LinkedField,
    snapshot: Dict[str, Any],
    snapshots: Dict[str, _OrderedStore],
    source: str,
    lineno: int,
    path: str,
) -> Optional[Dict[str, Any]]:
    """Follow one single-object @link to the latest target snapshot.

    Returns None when any local key value is null (the relationship itself is
    absent). Missing local key fields, values that cannot form the target
    primary key or a target key without a current snapshot end the stream with
    EventError.
    """
    local_values = _read_local(node, snapshot, source, lineno, path)
    if local_values is None:
        return None

    target_store = snapshots.get(node.target_table)
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
            f"{source}:{lineno}: no current snapshot of entity "
            f"'{node.target_table}' for '{path}' with key {{{rendered}}}",
        )
    _check_target_fields(node, related, source, lineno, path)
    return related


def _resolve_list(
    node: LinkedField,
    snapshot: Dict[str, Any],
    snapshots: Dict[str, _OrderedStore],
    entity_keys: Dict[str, List[str]],
    source: str,
    lineno: int,
    path: str,
) -> List[Dict[str, Any]]:
    """Match all current target snapshots for a list @link, in insert order.

    An empty list stands for both an absent relationship (any local value
    null) and a relationship without a current match. Local key fields that
    are missing or non-scalar, and matched target snapshots that lack a target
    field or a usable primary key, end the stream with EventError.
    """
    local_values = _read_local(node, snapshot, source, lineno, path)
    if local_values is None:
        return []

    target_store = snapshots.get(node.target_table)
    if target_store is None:
        return []
    pk_fields = entity_keys[node.target_table]
    expected = tuple(local_values)
    matches: List[Dict[str, Any]] = []
    candidates = list(target_store.rows.values()) + list(target_store.keyless)
    for related in candidates:
        _check_target_fields(node, related, source, lineno, path)
        values = tuple(related[name] for name in node.target)
        if values != expected or None in values:
            continue
        _check_primary_key(
            node.target_table, pk_fields, related, source, lineno, path
        )
        matches.append(related)
    return matches


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
