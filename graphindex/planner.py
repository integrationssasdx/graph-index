"""Turn an executable GraphQL operation into a physical query plan.

The plan describes one or more table scans (``roots``) joined by foreign-
key associations (``joins``). Response shape is captured by response paths
on each node: ``path`` is the alias hierarchy from the operation root and
``fields`` lists the leaf scalar paths a table is responsible for.

Example output::

    {
      "operationType": "query",
      "operationName": "GetUser",
      "roots": [{"path": ["user"], "entity": "users",
                 "filter": {"id": 7}, "fields": [["user", "id"]]}],
      "joins": [{"path": ["user", "team"], "fromEntity": "users",
                 "fromField": "team_id", "toEntity": "teams",
                 "toField": "id", "fields": [["user", "team", "name"]]}]
    }
"""

from . import ast_nodes as ast
from .errors import (InvalidJoin, InvalidQuery, InvalidRequest,
                     InvalidOperation, UnsupportedOperation, UnknownEntity,
                     UnknownField, VariablesError)


# ---------------------------------------------------------------------------
# Variable resolution / type checking
# ---------------------------------------------------------------------------

class VariableResolver:
    def __init__(self, variable_definitions, raw_variables, enum_values):
        self.defs = {d.variable.value: d for d in variable_definitions}
        self.values = raw_variables if isinstance(raw_variables, dict) else {}
        self.enums = enum_values  # enum name -> set of allowed values

    def resolve(self, variable_node):
        """Return the coerced value for an AST Variable node."""
        name = variable_node.name.value
        if name not in self.defs:
            # An operation may use variables it does not declare; GraphQL
            # would reject this, but treat as a missing variable here.
            raise VariablesError(f"variable '${name}' is not defined by the operation")
        definition = self.defs[name]
        if name not in self.values or self.values[name] is None:
            if definition.default is not None:
                return _const_to_python(definition.default)
            if isinstance(definition.type, ast.NonNullType):
                raise VariablesError(
                    f"missing required variable '${name}' "
                    f"of type {_type_str(definition.type)}")
            return None
        return self._coerce(name, definition.type, self.values[name])

    def _coerce(self, name, type_node, value):
        if isinstance(type_node, ast.NonNullType):
            if value is None:
                raise VariablesError(
                    f"variable '${name}' is null but type "
                    f"{_type_str(type_node)} is non-null")
            return self._coerce(name, type_node.type, value)
        if value is None:
            return None
        if isinstance(type_node, ast.ListType):
            if not isinstance(value, list):
                raise VariablesError(
                    f"variable '${name}' must be a list, got "
                    f"{_json_kind(value)}")
            return [self._coerce(name, type_node.type, item) for item in value]
        base = type_node.name.value
        return self._check_scalar(name, base, value)

    def _check_scalar(self, name, base, value):
        if base in ("ID", "String"):
            if not isinstance(value, str):
                if base == "ID" and isinstance(value, int) \
                        and not isinstance(value, bool):
                    return str(value)
                raise VariablesError(
                    f"variable '${name}' of type {base} must be a string, "
                    f"got {_json_kind(value)}")
            return value
        if base in ("Int", "BigInt", "BigDecimal"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise VariablesError(
                    f"variable '${name}' of type {base} must be an integer, "
                    f"got {_json_kind(value)}")
            return value
        if base == "Float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise VariablesError(
                    f"variable '${name}' of type Float must be a number, "
                    f"got {_json_kind(value)}")
            return float(value)
        if base == "Boolean":
            if not isinstance(value, bool):
                raise VariablesError(
                    f"variable '${name}' of type Boolean must be a boolean, "
                    f"got {_json_kind(value)}")
            return value
        if base in self.enums:
            if not isinstance(value, str):
                raise VariablesError(
                    f"variable '${name}' of enum {base} must be a string, "
                    f"got {_json_kind(value)}")
            allowed = self.enums[base]
            if value not in allowed:
                raise VariablesError(
                    f"variable '${name}' value '{value}' is not a member of "
                    f"enum {base}")
            return value
        # Unknown/custom scalar: pass JSON value through unchanged.
        return value


def _json_kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_str(type_node):
    if isinstance(type_node, ast.NonNullType):
        return _type_str(type_node.type) + "!"
    if isinstance(type_node, ast.ListType):
        return "[" + _type_str(type_node.type) + "]"
    return type_node.name.value


def _const_to_python(node):
    if isinstance(node, ast.IntValue):
        return node.value
    if isinstance(node, ast.FloatValue):
        return node.value
    if isinstance(node, ast.StringValue):
        return node.value
    if isinstance(node, ast.BooleanValue):
        return node.value
    if isinstance(node, ast.NullValue):
        return None
    if isinstance(node, ast.EnumValue):
        return node.value
    if isinstance(node, ast.ListValue):
        return [_const_to_python(v) for v in node.values]
    if isinstance(node, ast.ObjectValue):
        return {k: _const_to_python(v) for k, v in node.fields}
    raise VariablesError("unsupported default value in variable definition")


def _literal_to_python(node, resolve_variable=None):
    """Convert an argument literal to a plain value.

    Nested variables are resolved when *resolve_variable* is given; they
    cannot legally appear in constant positions.
    """
    if isinstance(node, ast.Variable):
        if resolve_variable is None:
            raise UnknownField("a variable was used in a constant position")
        return resolve_variable(node)
    if isinstance(node, ast.IntValue):
        return node.value
    if isinstance(node, ast.FloatValue):
        return node.value
    if isinstance(node, ast.StringValue):
        return node.value
    if isinstance(node, ast.BooleanValue):
        return node.value
    if isinstance(node, ast.NullValue):
        return None
    if isinstance(node, ast.EnumValue):
        return node.value
    if isinstance(node, ast.ListValue):
        return [_literal_to_python(v, resolve_variable) for v in node.values]
    if isinstance(node, ast.ObjectValue):
        return {k: _literal_to_python(v, resolve_variable)
                for k, v in node.fields}
    raise UnknownField("argument value has an unsupported shape")


# ---------------------------------------------------------------------------
# Operation selection
# ---------------------------------------------------------------------------

def select_operation(document, requested_name):
    operations = document.operations()
    if not operations:
        raise InvalidOperation("the document contains no operation")

    if requested_name is None and len(operations) == 1:
        # Exactly one operation in the document (named or anonymous, query
        # or mutation) is selected automatically.
        return operations[0]
    if requested_name is None:
        raise InvalidRequest(
            "the document defines multiple operations; an operation name is "
            "required")

    for op in operations:
        if op.name is not None and op.name.value == requested_name:
            return op
    names = ", ".join(sorted(op.name.value for op in operations
                             if op.name is not None)) or "<anonymous>"
    raise InvalidOperation(
        f"operation '{requested_name}' was not found; available: {names}")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class _Node:
    """A planned table access: a root scan or a join target."""

    def __init__(self, kind, path, entity_model, filter=None,
                 from_entity=None, from_field=None, to_field=None,
                 source_field=None):
        self.kind = kind                      # 'root' | 'join'
        self.path = tuple(path)               # response alias path
        self.entity = entity_model
        self.filter = filter if filter is not None else {}
        self.from_entity = from_entity        # EntityModel (join only)
        self.from_field = from_field          # local FK column (join only)
        self.to_field = to_field              # target key column (join only)
        self.source_field = source_field      # GraphQL field name selected
        self.fields = []                      # list[tuple[str]]
        self.field_sources = {}               # path -> source field name


class Planner:
    def __init__(self, mapping, document, variables):
        self.mapping = mapping
        self.document = document
        self.fragments = document.fragments()
        self.raw_variables = variables
        self.resolver = None
        self.nodes = []
        self._node_by_path = {}
        self._scalar_paths = set()
        self._expanding = []
        self._validate_document()

    def _validate_document(self):
        fragment_names = set()
        op_names = set()
        for definition in self.document.definitions:
            if isinstance(definition, ast.FragmentDefinition):
                fname = definition.name.value
                if fname in fragment_names:
                    raise InvalidQuery(
                        f"fragment '{fname}' is declared more than once")
                fragment_names.add(fname)
            elif isinstance(definition, ast.OperationDefinition):
                if definition.name is not None:
                    oname = definition.name.value
                    if oname in op_names:
                        raise InvalidRequest(
                            f"operation '{oname}' is declared more than once")
                    op_names.add(oname)
                seen_vars = set()
                for var_def in definition.variable_definitions:
                    vname = var_def.variable.value
                    if vname in seen_vars:
                        raise InvalidQuery(
                            f"variable '${vname}' is declared more than once")
                    seen_vars.add(vname)

    def plan(self, operation_name=None):
        operation = select_operation(self.document, operation_name)
        if operation.operation == "subscription":
            label = f" '{operation.name.value}'" if operation.name else ""
            raise UnsupportedOperation(
                f"subscription operations are not supported (operation{label})")
        if operation.operation not in ("query", "mutation"):
            raise InvalidOperation(
                f"unknown operation type '{operation.operation}'")

        self.resolver = VariableResolver(
            operation.variable_definitions, self.raw_variables,
            self.mapping.enums)

        root_type = self.mapping.root_types.get(operation.operation)
        self._walk_root_selection(operation, root_type)
        return self._serialize(operation)

    # -- root level ---------------------------------------------------------

    def _walk_root_selection(self, operation, root_type):
        self._walk_root_selections(
            operation.selection_set.selections, root_type)

    def _walk_root_selections(self, selections, root_type):
        if not selections:
            raise InvalidQuery("the operation selects no fields")
        for selection in selections:
            if isinstance(selection, ast.Field):
                self._plan_root_field(selection, root_type)
            elif isinstance(selection, ast.FragmentSpread):
                self._apply_root_fragment(selection.name.value, root_type)
            elif isinstance(selection, ast.InlineFragment):
                self._apply_root_inline(selection, root_type)

    def _apply_root_fragment(self, frag_name, root_type):
        if frag_name not in self.fragments:
            raise UnknownField(f"fragment '{frag_name}' is not defined")
        if frag_name in self._expanding:
            raise InvalidQuery(
                f"fragment '{frag_name}' forms a spread cycle")
        fragment = self.fragments[frag_name]
        self._expanding.append(frag_name)
        try:
            self._check_root_condition(
                fragment.type_condition, root_type,
                f"fragment '{frag_name}'")
            self._walk_root_selections(
                fragment.selection_set.selections, root_type)
        finally:
            self._expanding.pop()

    def _apply_root_inline(self, fragment, root_type):
        if fragment.type_condition is not None:
            self._check_root_condition(
                fragment.type_condition, root_type, "inline fragment")
        self._walk_root_selections(
            fragment.selection_set.selections, root_type)

    def _check_root_condition(self, condition, root_type, where):
        expected = root_type.name if root_type is not None else None
        if condition != expected:
            if not self._condition_known(condition) and condition != expected:
                raise UnknownEntity(
                    f"{where} targets unmapped type '{condition}'")
            raise InvalidQuery(
                f"{where} targets '{condition}' but the root type is "
                f"'{expected}'")

    def _plan_root_field(self, field, root_type):
        fname = field.name.value
        if fname == "__typename":
            # Meta field at root: no table access and no scalar column.
            return
        if root_type is None or fname not in root_type.fields:
            kind = root_type.kind if root_type is not None else "operation"
            raise UnknownField(
                f"{kind} root field '{fname}' is not defined in the schema")
        root_field = root_type.fields[fname]
        entity = self.mapping.entities[root_field.entity_name]

        filter_values = {}
        for arg in field.arguments:
            aname = arg.name.value
            if aname not in root_field.arg_types:
                raise UnknownField(
                    f"root field '{fname}' has no argument '{aname}'")
            value = self._arg_value(arg.value, aname, fname)
            filter_values[aname] = value

        alias = field.alias.value if field.alias else fname
        path = (alias,)
        existing = self._node_by_path.get(path)
        if existing is not None:
            if existing.source_field != fname:
                raise InvalidQuery(
                    f"conflicting fields '{existing.source_field}' and "
                    f"'{fname}' both use response path '{alias}'")
            self._merge_filter(existing, filter_values, path)
            self._expand_selection(field, existing.entity, existing)
            return
        node = _Node("root", path, entity, filter=filter_values,
                     source_field=fname)
        self.nodes.append(node)
        self._node_by_path[path] = node
        self._expand_selection(field, entity, node)

    def _merge_filter(self, node, new_filter, path):
        for key, value in new_filter.items():
            if key in node.filter and node.filter[key] != value:
                raise InvalidQuery(
                    f"field at response path "
                    f"{'/'.join(path)} receives conflicting values for "
                    f"argument '{key}'")
            node.filter[key] = value

    # -- entity level -------------------------------------------------------

    def _expand_selection(self, field, entity, parent_node):
        if field.selection_set is None:
            # An entity-typed field must have a selection set in GraphQL;
            # root fields return entities, so this is always invalid here.
            raise InvalidQuery(
                f"field '{field.name.value}' on entity '{entity.name}' "
                f"must have a selection set")
        selections = field.selection_set.selections
        if not selections:
            raise InvalidQuery(
                f"selection on '{field.name.value}' is empty")
        self._walk_entity_selections(selections, entity, parent_node)

    def _walk_entity_selections(self, selections, entity, node):
        for selection in selections:
            if isinstance(selection, ast.Field):
                self._plan_entity_field(selection, entity, node)
            elif isinstance(selection, ast.FragmentSpread):
                self._apply_fragment(selection.name.value, entity, node)
            elif isinstance(selection, ast.InlineFragment):
                self._apply_inline_fragment(selection, entity, node)

    def _apply_fragment(self, frag_name, entity, node):
        if frag_name not in self.fragments:
            raise UnknownField(f"fragment '{frag_name}' is not defined")
        if frag_name in self._expanding:
            raise InvalidQuery(
                f"fragment '{frag_name}' forms a spread cycle")
        fragment = self.fragments[frag_name]
        self._expanding.append(frag_name)
        try:
            self._enter_fragment_scope(fragment, entity, node)
        finally:
            self._expanding.pop()

    def _condition_matches(self, condition, entity):
        if condition == entity.name:
            return True
        return condition in entity.interfaces

    def _condition_known(self, condition):
        if condition in self.mapping.entities:
            return True
        return condition in self.mapping.interfaces

    def _enter_fragment_scope(self, fragment, entity, node):
        condition = fragment.type_condition
        if not self._condition_known(condition):
            raise UnknownEntity(
                f"fragment '{fragment.name.value}' targets unmapped "
                f"type '{condition}'")
        if not self._condition_matches(condition, entity):
            # Non-matching concrete-typed spread contributes nothing.
            return
        if not fragment.selection_set.selections:
            raise InvalidQuery(f"fragment '{fragment.name.value}' is empty")
        self._walk_entity_selections(
            fragment.selection_set.selections, entity, node)

    def _apply_inline_fragment(self, fragment, entity, node):
        if fragment.type_condition is not None:
            if not self._condition_known(fragment.type_condition):
                raise UnknownEntity(
                    f"inline fragment targets unmapped type "
                    f"'{fragment.type_condition}'")
            if not self._condition_matches(fragment.type_condition, entity):
                return  # non-matching condition: contributes nothing
        if not fragment.selection_set.selections:
            raise InvalidQuery("inline fragment has an empty selection")
        self._walk_entity_selections(
            fragment.selection_set.selections, entity, node)

    def _plan_entity_field(self, field, entity, node):
        fname = field.name.value
        if fname == "__typename":
            return
        if fname not in entity.fields:
            raise UnknownField(
                f"entity '{entity.name}' has no field '{fname}'")
        model = entity.fields[fname]
        alias = field.alias.value if field.alias else fname

        if model.is_link:
            if field.arguments:
                raise UnknownField(
                    f"linked field '{fname}' does not accept arguments")
            target_entity = self.mapping.entities.get(model.type_name)
            if target_entity is None:
                raise UnknownEntity(
                    f"link '{entity.name}.{fname}' points at unmapped "
                    f"type '{model.type_name}'")
            if model.link_target != target_entity.key:
                raise InvalidJoin(
                    f"@link on '{entity.name}.{fname}' targets "
                    f"'{target_entity.name}.{model.link_target}', which is "
                    f"not the entity's primary key '{target_entity.key}'")
            if field.selection_set is None:
                raise InvalidQuery(
                    f"linked field '{fname}' must have a selection set")
            if not field.selection_set.selections:
                raise InvalidQuery(
                    f"selection on linked field '{fname}' is empty")

            child_path = node.path + (alias,)
            if child_path in self._scalar_paths:
                raise InvalidQuery(
                    f"response path {'/'.join(child_path)} is used as both "
                    f"a scalar and an object")
            existing = self._node_by_path.get(child_path)
            if existing is not None:
                # Same response path reached again (e.g. via fragments):
                # verify it describes the same association, then merge.
                if existing.source_field != fname \
                        or existing.entity.name != target_entity.name \
                        or existing.from_field != model.link_local \
                        or existing.to_field != model.link_target:
                    raise InvalidQuery(
                        f"response path {'/'.join(child_path)} selects "
                        f"conflicting fields")
                child_node = existing
            else:
                child_node = _Node(
                    "join", child_path, target_entity,
                    from_entity=entity, from_field=model.link_local,
                    to_field=model.link_target, source_field=fname)
                self.nodes.append(child_node)
                self._node_by_path[child_path] = child_node
            self._walk_entity_selections(
                field.selection_set.selections, target_entity, child_node)
            return

        # Scalar field.
        if field.arguments:
            raise UnknownField(
                f"scalar field '{fname}' does not accept arguments")
        if field.selection_set is not None:
            raise InvalidQuery(
                f"scalar field '{fname}' must not have a selection set")
        leaf_path = node.path + (alias,)
        if leaf_path in self._node_by_path:
            raise InvalidQuery(
                f"response path {'/'.join(leaf_path)} is used as both an "
                f"object and a scalar")
        previous_source = node.field_sources.get(leaf_path)
        if previous_source is None:
            node.fields.append(leaf_path)
            node.field_sources[leaf_path] = fname
            self._scalar_paths.add(leaf_path)
        elif previous_source != fname:
            raise InvalidQuery(
                f"response path {'/'.join(leaf_path)} aliases conflicting "
                f"fields '{previous_source}' and '{fname}'")

    # -- arguments ----------------------------------------------------------

    def _arg_value(self, value_node, aname, root_fname):
        if isinstance(value_node, ast.Variable):
            return self.resolver.resolve(value_node)
        return _literal_to_python(
            value_node, resolve_variable=self.resolver.resolve)

    # -- output -------------------------------------------------------------

    def _serialize(self, operation):
        roots, joins = [], []
        for node in self.nodes:
            entry = {
                "path": list(node.path),
                "entity": node.entity.table,
                "filter": node.filter,
                "fields": [list(p) for p in node.fields],
            }
            if node.kind == "root":
                roots.append(entry)
            else:
                joins.append({
                    "path": list(node.path),
                    "fromEntity": node.from_entity.table,
                    "fromField": node.from_field,
                    "toEntity": node.entity.table,
                    "toField": node.to_field,
                    "fields": [list(p) for p in node.fields],
                })
        return {
            "operationType": operation.operation,
            "operationName": operation.name.value if operation.name else None,
            "roots": roots,
            "joins": joins,
        }


def build_plan(mapping, document, variables, operation_name=None):
    return Planner(mapping, document, variables).plan(operation_name)
