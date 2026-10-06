"""Query complexity control.

Computes a deterministic request-complexity score from the same GraphQL
document and variables used by execution, strictly *before* any resolver,
indexed-entity read or subscription is established. Over-limit requests stop
with a single ``QUERY_COMPLEXITY_EXCEEDED`` GraphQL error without touching the
entity snapshots.

Scoring rules
-------------
* the selected operation is walked from its root selection set;
* every leaf field costs 1; a composite field costs only the sum of its
  children (the object container itself is free);
* repeated fields and distinct aliases are scored independently;
* fragments are inlined once at each spread position; an inline fragment
  contributes its selection directly;
* introspection fields ``__schema`` and ``__type`` cost 0;
* list fields scale their sub-selection by an effective item upper bound read
  from a ``first`` or ``limit`` argument -- a literal positive integer first,
  then a positive-integer variable of the same name -- falling back to
  ``DEFAULT_LIST_BOUND`` when the argument is absent, non-positive or
  non-integer. Variable values keep their GraphQL type validation from the
  existing validation phase; the scorer never rejects one (a non-integer bound
  simply means "no usable bound").

The scorer walks the raw AST rather than the planner's merged selection so the
"repeated fields count separately" requirement holds regardless of the
response-key de-duplication execution performs.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from .errors import PlanError
from .gql import (
    FieldNode,
    FragmentSpread,
    InlineFragment,
    Var,
    parse_executable,
)
from .planner import Planner, _is_list_type
from .schema import Schema, TypeInfo, named_of

DEFAULT_LIST_BOUND = 1000
LIMIT_ENV_VAR = "GRAPHQL_QUERY_COMPLEXITY_LIMIT"

# Argument names that may supply a list item upper bound, in priority order.
BOUND_ARG_NAMES = ("first", "limit")


class QueryComplexityExceeded(PlanError):
    """The selected operation scores strictly above the configured limit.

    Rendered as a GraphQL response on stdout (``data: null`` plus a single
    error whose ``extensions.code`` is QUERY_COMPLEXITY_EXCEEDED) rather than
    the usual ``{code, message}`` stderr envelope.
    """

    def __init__(self, complexity: int, limit: int):
        super().__init__(
            "QUERY_COMPLEXITY_EXCEEDED",
            f"query complexity {complexity} exceeds the configured limit {limit}",
        )
        self.complexity = complexity
        self.limit = limit


def complexity_error_payload(complexity: int, limit: int) -> Dict[str, Any]:
    """Build the GraphQL-style response emitted for an over-limit request."""
    return {
        "data": None,
        "errors": [
            {
                "message": (
                    f"query complexity {complexity} exceeds the configured "
                    f"limit {limit}"
                ),
                "extensions": {"code": "QUERY_COMPLEXITY_EXCEEDED"},
            }
        ],
    }


def load_complexity_limit(raw: Optional[str]) -> Optional[int]:
    """Validate a GRAPHQL_QUERY_COMPLEXITY_LIMIT value.

    Returns ``None`` when the control is disabled (the variable is unset or
    ``"0"``), otherwise the positive integer limit. A negative, non-integer or
    otherwise unparseable value raises INVALID_QUERY_COMPLEXITY_LIMIT.
    """
    if raw is None:
        return None
    text = raw.strip()
    try:
        value = int(text)
    except ValueError:
        raise PlanError(
            "INVALID_QUERY_COMPLEXITY_LIMIT",
            f"value {raw!r} must be a non-negative integer",
        )
    if value < 0:
        raise PlanError(
            "INVALID_QUERY_COMPLEXITY_LIMIT",
            "query complexity limit must not be negative",
        )
    if value == 0:
        return None
    return value


def load_limit_from_env(
    environ: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[int]:
    """Read and validate the limit from the process environment."""
    env = os.environ if environ is None else environ
    return load_complexity_limit(env.get(LIMIT_ENV_VAR))


class ComplexityCalculator(Planner):
    """Scores one selected operation using the planner's type knowledge.

    Only fragment type-condition matching is inherited; the selection walk is
    intentionally separate so it never merges repeated fields.
    """

    def score_operation(self, op, fragments: Dict[str, Any]) -> int:
        self.fragments = fragments
        self.var_map = {}
        for name, type_ref, default, has_default in op.var_defs:
            if name in self.var_map:
                raise PlanError(
                    "VariablesError",
                    f"variable '${name}' is declared more than once",
                )
            self.var_map[name] = (type_ref, default, has_default)

        root_name = self.schema.roots.get(op.op_type)
        root_info = self.schema.types.get(root_name) if root_name else None
        if root_info is None:
            raise PlanError(
                "UnknownField",
                f"schema does not define a root type for {op.op_type} operations",
            )
        return self._walk_set(op.selection_set, root_info, [])

    # -- list bounds ----------------------------------------------------------

    def _resolve_bound(self, value: Any) -> Optional[int]:
        """Resolve a first/limit argument to a positive integer, else None."""
        if isinstance(value, Var):
            spec = self.var_map.get(value.name)
            if spec is None:
                # An undeclared variable is reported by the regular validation
                # phase; complexity treats it as "no usable bound".
                return None
            _type_ref, default, has_default = spec
            if value.name in self.variables:
                value = self.variables[value.name]
            elif has_default:
                value = default
            else:
                return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        return None

    def _bound_for(self, node: FieldNode) -> int:
        """Effective item upper bound for a list field."""
        for arg_name in BOUND_ARG_NAMES:
            if arg_name in node.args:
                bound = self._resolve_bound(node.args[arg_name])
                return bound if bound is not None else DEFAULT_LIST_BOUND
        return DEFAULT_LIST_BOUND

    # -- selection walk -------------------------------------------------------

    def _walk_set(self, selections, info: TypeInfo, stack: List[str]) -> int:
        return sum(self._walk_one(sel, info, stack) for sel in selections)

    def _walk_one(self, sel, info: TypeInfo, stack: List[str]) -> int:
        if isinstance(sel, FieldNode):
            return self._walk_field(sel, info, stack)
        if isinstance(sel, FragmentSpread):
            frag = self.fragments.get(sel.name)
            if frag is None:
                raise PlanError(
                    "InvalidQuery", f"unknown fragment '{sel.name}'"
                )
            if sel.name in stack:
                raise PlanError(
                    "InvalidQuery", f"fragment cycle involving '{sel.name}'"
                )
            if not self._type_condition_matches(frag.type_condition, info.name):
                return 0
            frag_info = self.schema.types.get(frag.type_condition, info)
            return self._walk_set(
                frag.selection_set, frag_info, stack + [sel.name]
            )
        if isinstance(sel, InlineFragment):
            if sel.type_condition is None:
                return self._walk_set(sel.selection_set, info, stack)
            if not self._type_condition_matches(sel.type_condition, info.name):
                return 0
            inner_info = self.schema.types.get(sel.type_condition, info)
            return self._walk_set(sel.selection_set, inner_info, stack)
        return 0

    def _walk_field(self, node: FieldNode, info: TypeInfo, stack: List[str]) -> int:
        if node.name in ("__schema", "__type"):
            return 0
        field = info.fields.get(node.name)
        if field is None:
            raise PlanError(
                "UnknownField",
                f"unknown field '{node.name}' on type '{info.name}'",
            )
        type_name = named_of(field.type_ref)
        if self.schema.is_leaf(type_name):
            return 1
        target_info = self.schema.types.get(type_name)
        if target_info is None:
            raise PlanError(
                "UnknownField",
                f"field '{node.name}' has unsupported type '{type_name}'",
            )
        if node.selection_set is None:
            raise PlanError(
                "InvalidQuery", f"field '{node.name}' requires a selection set"
            )
        inner = self._walk_set(node.selection_set, target_info, stack)
        if _is_list_type(field.type_ref):
            inner *= self._bound_for(node)
        return inner


def enforce_complexity(
    schema: Schema,
    variables: Dict[str, Any],
    query_text: str,
    source: str,
    operation_name: Optional[str],
    select_operation: Callable,
    limit: int,
) -> int:
    """Re-parse the document, select the operation and enforce the limit.

    Invoked only after the command's own compiler has validated the document,
    so any GraphQL/variable/mapping error has already surfaced with its
    existing code. Raises QueryComplexityExceeded when the score is strictly
    above ``limit``; returns the score otherwise.
    """
    operations, fragments = parse_executable(query_text, source)
    op = select_operation(operations, operation_name)
    calculator = ComplexityCalculator(schema, variables)
    complexity = calculator.score_operation(op, fragments)
    if complexity > limit:
        raise QueryComplexityExceeded(complexity, limit)
    return complexity
