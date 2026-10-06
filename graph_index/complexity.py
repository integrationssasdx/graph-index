"""Query complexity control.

Complexity is measured from the selected operation's GraphQL document and
variables, after the document has passed the existing GraphQL validation
performed by the query/subscription compilers:

* every leaf field counts 1;
* a composite field only accumulates the complexity of its subselection;
* repeated fields and distinct aliases are counted separately;
* a fragment spread is expanded once in place (fragment definitions never
  reached from the selected operation are not counted);
* the introspection fields ``__schema`` and ``__type`` count 0, including
  their subselection;
* a list field multiplies its subselection by an upper bound on the number
  of entries: the positive integer value of a ``first`` or ``limit``
  argument (literal or variable), defaulting to 1000 when the argument is
  absent, non-positive or non-integer. GraphQL type validation of the
  variable itself stays with the existing validation stage.

The configured limit comes from the ``GRAPHQL_QUERY_COMPLEXITY_LIMIT``
environment variable: unset/empty disables the feature, ``0`` disables it
too, a positive integer is the per-operation maximum, and anything else
(negative numbers, non-integers, unparseable values) fails startup with
code ``INVALID_QUERY_COMPLEXITY_LIMIT``.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from .errors import PlanError
from .gql import (
    FieldNode,
    FragmentSpread,
    InlineFragment,
    Operation,
    Var,
)
from .schema import Schema, TypeInfo, named_of

ENV_VAR = "GRAPHQL_QUERY_COMPLEXITY_LIMIT"

DEFAULT_LIST_BOUND = 1000
BOUND_ARGUMENTS = ("first", "limit")
INTROSPECTION_FIELDS = frozenset({"__schema", "__type"})

TypeChecker = Callable[[Any, Any], bool]


class QueryComplexityExceeded(PlanError):
    """Raised when an operation measures strictly above the configured limit.

    Complexity equal to the limit is allowed; only strictly greater values
    stop the request.
    """

    def __init__(self, complexity: int, limit: int):
        super().__init__(
            "QUERY_COMPLEXITY_EXCEEDED",
            f"query complexity {complexity} exceeds the limit of {limit}",
        )
        self.complexity = complexity
        self.limit = limit


def load_complexity_limit(environ: Optional[Dict[str, str]] = None) -> Optional[int]:
    """Read and validate GRAPHQL_QUERY_COMPLEXITY_LIMIT at startup.

    Returns None when the variable is unset/empty (feature disabled), 0 when
    explicitly set to "0" (also disabled), and a positive int otherwise.
    Negative numbers, non-integers and unparseable values raise
    PlanError("INVALID_QUERY_COMPLEXITY_LIMIT") so the caller fails startup
    before opening any service port.
    """
    if environ is None:
        environ = os.environ
    raw = environ.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        return None
    text = raw.strip()
    if not text.lstrip("-").isdigit():
        raise PlanError(
            "INVALID_QUERY_COMPLEXITY_LIMIT",
            f"{ENV_VAR}={raw!r} is not a non-negative integer",
        )
    value = int(text)
    if value < 0:
        raise PlanError(
            "INVALID_QUERY_COMPLEXITY_LIMIT",
            f"{ENV_VAR}={raw!r} must not be negative",
        )
    return value


def limit_enabled(limit: Optional[int]) -> bool:
    return limit is not None and limit > 0


def resolve_bound_argument(
    value: Any,
    var_map: Optional[Dict[str, tuple]],
    variables: Dict[str, Any],
    check_type: Optional[TypeChecker] = None,
) -> int:
    """Resolve one first/limit argument value to a positive-integer bound.

    Literals that are not positive integers fall back to 1000. Variables are
    validated against their declared GraphQL type through ``check_type``
    exactly as in the existing validation stage; an undeclared variable or
    one with no value and no default is a VariablesError.
    """
    if not isinstance(value, Var):
        return _positive_bound(value)
    name = value.name
    if var_map is None or name not in var_map:
        raise PlanError(
            "VariablesError",
            f"variable '${name}' is not declared by the operation",
        )
    type_ref, default, has_default = var_map[name]
    if name in variables:
        provided = variables[name]
        if check_type is not None and not check_type(type_ref, provided):
            raise PlanError(
                "VariablesError",
                f"variable '${name}' has a value of the wrong type",
            )
        return _positive_bound(provided)
    if has_default:
        return _positive_bound(default)
    # Mirror the existing validation stage: a referenced variable with
    # no value and no default is a VariablesError.
    raise PlanError("VariablesError", f"variable '${name}' was not provided")


def resolve_list_bound(
    node: FieldNode,
    var_map: Optional[Dict[str, tuple]],
    variables: Dict[str, Any],
    check_type: Optional[TypeChecker] = None,
) -> int:
    """Effective positive-integer entry upper bound for one list field.

    ``first`` takes priority over ``limit``. A missing argument, a
    non-positive or non-integer value falls back to 1000.
    """
    for arg_name in BOUND_ARGUMENTS:
        if arg_name in node.args:
            return resolve_bound_argument(
                node.args[arg_name], var_map, variables, check_type
            )
    return DEFAULT_LIST_BOUND


def _positive_bound(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_LIST_BOUND
    if value <= 0:
        return DEFAULT_LIST_BOUND
    return value


def measure_complexity(
    schema: Schema,
    op: Operation,
    fragments: Dict[str, Any],
    variables: Dict[str, Any],
    var_map: Optional[Dict[str, tuple]] = None,
    check_type: Optional[TypeChecker] = None,
) -> int:
    """Compute the complexity of one selected operation.

    Only ``op``'s root selection set is traversed; other operations in the
    same document contribute nothing. The walk uses the raw AST, so repeated
    fields and aliases are counted separately rather than merged the way the
    query planner merges them.
    """
    root_type_name = schema.roots.get(op.op_type)
    root_type = schema.types.get(root_type_name) if root_type_name else None
    if root_type is None:
        return 0
    walker = _ComplexityWalker(schema, fragments, variables, var_map, check_type)
    return walker.selection_cost(op.selection_set, root_type, [])


class _ComplexityWalker:
    def __init__(
        self,
        schema: Schema,
        fragments: Dict[str, Any],
        variables: Dict[str, Any],
        var_map: Optional[Dict[str, tuple]],
        check_type: Optional[TypeChecker],
    ):
        self.schema = schema
        self.fragments = fragments
        self.variables = variables
        self.var_map = var_map
        self.check_type = check_type

    def selection_cost(
        self, selections: List[Any], type_info: TypeInfo, stack: List[str]
    ) -> int:
        total = 0
        for sel in selections:
            if isinstance(sel, FieldNode):
                total += self.field_cost(sel, type_info, stack)
            elif isinstance(sel, FragmentSpread):
                frag = self.fragments.get(sel.name)
                # The compiler already rejected unknown/cyclic spreads; skip
                # defensively so measurement never masks those errors.
                if frag is None or sel.name in stack:
                    continue
                if self._type_condition_matches(frag.type_condition, type_info.name):
                    total += self.selection_cost(
                        frag.selection_set, type_info, stack + [sel.name]
                    )
            elif isinstance(sel, InlineFragment):
                if sel.type_condition is None or self._type_condition_matches(
                    sel.type_condition, type_info.name
                ):
                    total += self.selection_cost(sel.selection_set, type_info, stack)
        return total

    def _type_condition_matches(self, condition: str, type_name: str) -> bool:
        if condition == type_name:
            return True
        info = self.schema.types.get(type_name)
        if info is not None and condition in info.interfaces:
            return True
        members = self.schema.unions.get(condition)
        return bool(members) and type_name in members

    def field_cost(self, node: FieldNode, parent: TypeInfo,
                   stack: List[str]) -> int:
        if node.name in INTROSPECTION_FIELDS:
            # Introspection fields count 0 together with their subselection.
            return 0
        field = parent.fields.get(node.name)
        if field is None:
            # Unknown fields are reported by the existing validation stage;
            # complexity never rewrites them.
            return 0
        if node.selection_set is None:
            return 1
        target_name = named_of(field.type_ref)
        target = self.schema.types.get(target_name)
        if target is None:
            # Leaf/enum types cannot carry selections in validated documents;
            # sum the written leaves without a schema-driven multiplier.
            inner = self._flat_cost(node.selection_set, stack)
        else:
            inner = self.selection_cost(node.selection_set, target, stack)
            if self._is_list(field.type_ref):
                inner *= resolve_list_bound(
                    node, self.var_map, self.variables, self.check_type
                )
        return inner

    def _flat_cost(self, selections: List[Any], stack: List[str]) -> int:
        total = 0
        for sel in selections:
            if isinstance(sel, FieldNode):
                if sel.name in INTROSPECTION_FIELDS:
                    continue
                if sel.selection_set is None:
                    total += 1
                else:
                    total += self._flat_cost(sel.selection_set, stack)
            elif isinstance(sel, FragmentSpread):
                frag = self.fragments.get(sel.name)
                if frag is not None and sel.name not in stack:
                    total += self._flat_cost(
                        frag.selection_set, stack + [sel.name]
                    )
            elif isinstance(sel, InlineFragment):
                total += self._flat_cost(sel.selection_set, stack)
        return total

    @staticmethod
    def _is_list(type_ref) -> bool:
        if type_ref[0] == "non_null":
            type_ref = type_ref[1]
        return type_ref[0] == "list"
