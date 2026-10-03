"""Subscription push: match entity change events against a subscription.

Compiles a subscription operation into an equality filter plus a leaf-field
projection over a single @entity root, then applies it to an NDJSON stream of
entity change events. GraphQL, variable and @entity mapping semantics are
shared with the query planner; only the supported selection shape is narrower
(root entity scalar/enum leaf fields only).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .errors import PlanError
from .gql import parse_executable
from .planner import Planner
from .schema import named_of

EVENT_OPS = ("INSERT", "UPDATE", "DELETE")


class SubscriptionPlan:
    """A compiled subscription ready to filter and project event snapshots."""

    __slots__ = ("operation_name", "path", "entity_table", "filter", "projections")

    def __init__(
        self,
        operation_name: Optional[str],
        path: str,
        entity_table: str,
        filter_obj: Dict[str, Any],
        projections: List[Tuple[str, str]],
    ):
        self.operation_name = operation_name
        self.path = path
        self.entity_table = entity_table
        self.filter = filter_obj  # entity field name -> expected value
        self.projections = projections  # (response key, entity field name) pairs

    def matches(self, snapshot: Dict[str, Any]) -> bool:
        for name, expected in self.filter.items():
            if name not in snapshot:
                raise PlanError(
                    "EventError", f"snapshot is missing filter field '{name}'"
                )
            if snapshot[name] != expected:
                return False
        return True

    def project(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for key, field_name in self.projections:
            if field_name not in snapshot:
                raise PlanError(
                    "EventError", f"snapshot is missing field '{field_name}'"
                )
            data[key] = snapshot[field_name]
        return data


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
        projections = self._compile_projection(target, node.selection_set, key)
        return SubscriptionPlan(op.name, key, entity.table, filter_obj, projections)

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

    # -- projection ---------------------------------------------------------------

    def _compile_projection(
        self, target, selection_set, path: str
    ) -> List[Tuple[str, str]]:
        projections: List[Tuple[str, str]] = []
        seen = set()
        for node in self._expand(selection_set, target.name, []):
            key = node.alias or node.name
            field = target.fields.get(node.name)
            if field is None:
                raise PlanError(
                    "InvalidQuery",
                    f"unknown field '{node.name}' on type '{target.name}'",
                )
            if not self.schema.is_leaf(named_of(field.type_ref)):
                raise PlanError(
                    "UnsupportedSelection",
                    f"nested field '{node.name}' is not supported in subscriptions",
                )
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
                projections.append((key, node.name))
        if not projections:
            raise PlanError("InvalidQuery", f"empty selection set at '{path}'")
        return projections


def push_events(
    plan: SubscriptionPlan, events_text: str, source: str
) -> List[Dict[str, Any]]:
    """Apply a compiled subscription to an NDJSON event stream, in order."""
    outputs: List[Dict[str, Any]] = []
    for lineno, raw in enumerate(events_text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        event = _parse_event(line, source, lineno)
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
        outputs.append(
            {
                "subscription": plan.operation_name,
                "path": plan.path,
                "event": op,
                "entity": event["entity"],
                "data": plan.project(snapshot),
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
