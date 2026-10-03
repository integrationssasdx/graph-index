"""Subscription push: match entity change events against a subscription.

Compiles a single-root-field subscription over an @entity mapped type into a
filter plus a leaf-field projection, then streams events.ndjson lines and
emits one JSON object per matching change. Only root entity scalar/enum leaf
fields are supported; nested selections (@link or otherwise) raise
PlanError "UnsupportedSelection". Malformed event lines raise "EventError".
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .errors import PlanError
from .gql import parse_executable
from .planner import Planner
from .schema import named_of

OPS = ("INSERT", "UPDATE", "DELETE")


class SubscriptionPlan:
    def __init__(
        self,
        operation_name: Optional[str],
        path: str,
        entity: str,
        filters: Dict[str, Any],
        fields: List[Tuple[str, str]],
    ):
        self.operation_name = operation_name
        self.path = path
        self.entity = entity
        self.filters = filters
        self.fields = fields  # (response key, entity field name) in selection order


class SubscriptionPlanner(Planner):
    """Reuses the query planner's fragment expansion and variable resolution."""

    def plan_subscription(
        self, text: str, source: str, operation_name: Optional[str]
    ) -> SubscriptionPlan:
        operations, self.fragments = parse_executable(text, source)
        op = self._select_subscription(operations, operation_name)
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
                "MappingError", "schema does not define a subscription root type"
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

        filters: Dict[str, Any] = {}
        for arg_name, value in node.args.items():
            target_field = target.fields.get(arg_name)
            if target_field is None or not self.schema.is_leaf(
                named_of(target_field.type_ref)
            ):
                raise PlanError(
                    "InvalidQuery",
                    f"argument '{arg_name}' does not match a filterable field of entity '{entity.table}'",
                )
            filters[arg_name] = self._resolve(value)

        leaf_selections = self._expand(node.selection_set, target.name, [])
        if not leaf_selections:
            raise PlanError("InvalidQuery", f"empty selection set at '{key}'")
        fields: List[Tuple[str, str]] = []
        for fnode in leaf_selections:
            fdef = target.fields.get(fnode.name)
            if fdef is None:
                raise PlanError(
                    "InvalidQuery",
                    f"unknown field '{fnode.name}' on type '{target.name}'",
                )
            if not self.schema.is_leaf(named_of(fdef.type_ref)):
                raise PlanError(
                    "UnsupportedSelection",
                    f"nested field '{fnode.name}' is not supported in subscriptions",
                )
            if fnode.selection_set is not None:
                raise PlanError(
                    "InvalidQuery",
                    f"scalar field '{fnode.name}' must not have a selection set",
                )
            if fnode.args:
                raise PlanError(
                    "InvalidQuery",
                    f"arguments on field '{fnode.name}' are not supported",
                )
            fields.append((fnode.alias or fnode.name, fnode.name))

        return SubscriptionPlan(op.name, key, entity.table, filters, fields)

    @staticmethod
    def _select_subscription(operations, operation_name):
        if operation_name is not None:
            for op in operations:
                if op.name == operation_name and op.op_type == "subscription":
                    return op
            raise PlanError(
                "InvalidQuery", f"no subscription operation named '{operation_name}'"
            )
        if not operations:
            raise PlanError("InvalidQuery", "document contains no operations")
        if len(operations) > 1:
            raise PlanError(
                "InvalidQuery",
                "multiple operations found; select one with --operation",
            )
        op = operations[0]
        if op.op_type != "subscription":
            raise PlanError("InvalidQuery", "operation must be a subscription")
        return op


def _event_error(source: str, lineno: int, message: str) -> PlanError:
    return PlanError("EventError", f"{source}:{lineno}: {message}")


def iter_notifications(
    plan: SubscriptionPlan, events_text: str, source: str
) -> Iterator[dict]:
    """Yield one notification object per matching event line, in order."""
    for lineno, line in enumerate(events_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _event_error(source, lineno, f"invalid JSON: {exc.msg}")
        if not isinstance(event, dict):
            raise _event_error(source, lineno, "event line must be a JSON object")
        for key in ("op", "entity", "before", "after"):
            if key not in event:
                raise _event_error(source, lineno, f"event is missing '{key}'")
        op = event["op"]
        if op not in OPS:
            raise _event_error(source, lineno, f"unknown op {op!r}")
        entity = event["entity"]
        if not isinstance(entity, str):
            raise _event_error(source, lineno, "'entity' must be a string")
        before, after = event["before"], event["after"]
        for label, snapshot in (("before", before), ("after", after)):
            if snapshot is not None and not isinstance(snapshot, dict):
                raise _event_error(source, lineno, f"'{label}' must be an object or null")

        if entity != plan.entity:
            continue
        snapshot = before if op == "DELETE" else after
        if not isinstance(snapshot, dict):
            raise _event_error(source, lineno, f"{op} event has no entity snapshot")
        if any(snapshot.get(name) != value for name, value in plan.filters.items()):
            continue
        yield {
            "subscription": plan.operation_name,
            "path": plan.path,
            "event": op,
            "entity": entity,
            "data": {key: snapshot.get(name) for key, name in plan.fields},
        }
