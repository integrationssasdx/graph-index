"""Entity mapping derived from an SDL schema.

The mapping is driven by two directives:

* ``@entity(name: "table", key: "id")`` marks an object type as a stored
  entity, naming its table and primary-key column.
* ``@link(local: "team_id", target: "id")`` marks a field as a foreign-key
  association, naming the local column and the target entity's key column.

Everything is validated up front so the planner can assume a coherent
mapping; see :mod:`graphindex.errors` for the meaning of each code.
"""

from . import ast_nodes as ast
from .errors import InvalidJoin, MappingError, UnknownEntity


class FieldModel:
    __slots__ = ("name", "type_name", "is_list", "is_nonnull",
                 "link_local", "link_target")

    def __init__(self, name, type_name, is_list, is_nonnull):
        self.name = name
        self.type_name = type_name          # base named type
        self.is_list = is_list
        self.is_nonnull = is_nonnull
        self.link_local = None              # local FK column when linked
        self.link_target = None             # target key column when linked

    @property
    def is_link(self):
        return self.link_local is not None


class EntityModel:
    __slots__ = ("name", "table", "key", "fields", "interfaces")

    def __init__(self, name, table, key):
        self.name = name                    # GraphQL type name
        self.table = table                  # @entity name
        self.key = key                      # primary-key field name
        self.fields = {}                    # str -> FieldModel
        self.interfaces = ()                # interface names implemented


class RootFieldModel:
    __slots__ = ("name", "entity_name", "arg_types")

    def __init__(self, name, entity_name, arg_types):
        self.name = name
        self.entity_name = entity_name
        # Ordered argument name -> InputValueDefinition (for type checks).
        self.arg_types = arg_types


class RootTypeModel:
    def __init__(self, name, kind):
        self.name = name                    # GraphQL type name, e.g. "Query"
        self.kind = kind                    # 'query' | 'mutation' | 'subscription'
        self.fields = {}                    # str -> RootFieldModel


class Mapping:
    def __init__(self):
        self.entities = {}                  # GraphQL name -> EntityModel
        self.tables = {}                    # table name -> GraphQL name
        self.enums = {}                     # enum name -> set of values
        self.interfaces = set()             # declared interface type names
        self.root_types = {}                # kind -> RootTypeModel

    def entity_for_table(self, table):
        graphql_name = self.tables.get(table)
        return self.entities.get(graphql_name) if graphql_name else None


def _directive(node, name):
    found = [d for d in getattr(node, "directives", [])
             if d.name.value == name]
    return found


def _require_single_directive(node, name, where):
    found = _directive(node, name)
    if not found:
        raise MappingError(f"{where} is missing a @{name} directive")
    if len(found) > 1:
        raise MappingError(f"{where} declares @{name} more than once")
    return found[0]


def _string_arg(directive, arg_name, where):
    for arg in directive.arguments:
        if arg.name.value == arg_name:
            value = arg.value
            if not isinstance(value, ast.StringValue):
                raise MappingError(
                    f"{where}: @{directive.name.value}({arg_name}:) "
                    f"must be a string literal")
            if not value.value:
                raise MappingError(
                    f"{where}: @{directive.name.value}({arg_name}:) "
                    f"must not be empty")
            return value.value
    raise MappingError(
        f"{where}: @{directive.name.value} is missing argument "
        f"'{arg_name}'")


def _type_shape(type_node):
    """Return (base_name, is_list, is_nonnull) for a type reference."""
    is_nonnull = isinstance(type_node, ast.NonNullType)
    if is_nonnull:
        type_node = type_node.type
    is_list = isinstance(type_node, ast.ListType)
    if is_list:
        # Unwrap list (and item non-null) to the named base.
        inner = type_node.type
        if isinstance(inner, ast.NonNullType):
            inner = inner.type
        base = inner.name.value
    else:
        base = type_node.name.value
    return base, is_list, is_nonnull


def _merge_extension(base, ext):
    """Merge an ``extend`` node's members into its base definition."""
    if type(base) is not type(ext):
        raise MappingError(
            f"extension of '{base.name}' has a different kind than its "
            f"base definition")
    base.directives = list(getattr(base, "directives", [])) + \
        list(getattr(ext, "directives", []))
    if isinstance(ext, (ast.ObjectTypeDefinition,
                        ast.InterfaceTypeDefinition,
                        ast.InputObjectTypeDefinition)):
        existing = {f.name.value for f in base.fields}
        for field in ext.fields:
            if field.name.value in existing:
                raise MappingError(
                    f"extension field '{base.name}.{field.name.value}' "
                    f"conflicts with an existing field")
            existing.add(field.name.value)
            base.fields.append(field)
        if isinstance(ext, ast.ObjectTypeDefinition):
            base.interfaces = tuple(
                dict.fromkeys(list(base.interfaces) + list(ext.interfaces)))
    elif isinstance(ext, ast.EnumTypeDefinition):
        base.values.extend(ext.values)
    elif isinstance(ext, ast.UnionTypeDefinition):
        base.members = list(dict.fromkeys(list(base.members)
                                          + list(ext.members)))


def build_mapping(document: ast.Document) -> Mapping:
    mapping = Mapping()
    type_defs = {}

    # First pass: index definitions, rejecting duplicate type names while
    # merging type extensions into their base definition.
    for node in document.definitions:
        if not isinstance(node, (ast.ObjectTypeDefinition,
                                 ast.InterfaceTypeDefinition,
                                 ast.InputObjectTypeDefinition,
                                 ast.EnumTypeDefinition,
                                 ast.UnionTypeDefinition,
                                 ast.ScalarTypeDefinition)):
            continue
        existing = type_defs.get(node.name)
        if getattr(node, "is_extension", False):
            if existing is None:
                raise MappingError(
                    f"extension of type '{node.name}' has no base definition")
            _merge_extension(existing, node)
            continue
        if existing is not None:
            raise MappingError(
                f"type '{node.name}' is declared more than once")
        type_defs[node.name] = node
        if isinstance(node, ast.InterfaceTypeDefinition):
            mapping.interfaces.add(node.name)
        if isinstance(node, ast.EnumTypeDefinition):
            values = {v.value for v in node.values}
            if len(values) != len(node.values):
                raise MappingError(
                    f"enum '{node.name}' declares a value more than once")
            mapping.enums[node.name] = values

    # Enum value sets after extension merging.
    for node in type_defs.values():
        if isinstance(node, ast.EnumTypeDefinition):
            values = [v.value for v in node.values]
            if len(set(values)) != len(values):
                raise MappingError(
                    f"enum '{node.name}' declares a value more than once")
            mapping.enums[node.name] = set(values)

    # Determine the root type names (explicit schema definition or defaults).
    root_names = {"query": "Query", "mutation": "Mutation",
                  "subscription": "Subscription"}
    for node in document.definitions:
        if isinstance(node, ast.SchemaDefinition):
            for kind, type_name in node.operation_types:
                root_names[kind] = type_name

    # Second pass: entities and their fields.
    for node in type_defs.values():
        if not isinstance(node, ast.ObjectTypeDefinition):
            continue
        entity_directives = _directive(node, "entity")
        if not entity_directives:
            continue  # plain object (e.g. a root type) is not an entity
        directive = _require_single_directive(node, "entity",
                                              f"type '{node.name}'")
        table = _string_arg(directive, "name", f"type '{node.name}'")
        key = _string_arg(directive, "key", f"type '{node.name}'")

        if table in mapping.tables:
            raise MappingError(
                f"table '{table}' is mapped by more than one entity "
                f"('{mapping.tables[table]}' and '{node.name}')")

        entity = EntityModel(node.name, table, key)
        entity.interfaces = tuple(node.interfaces)
        seen_fields = set()
        for field in node.fields:
            fname = field.name.value
            if fname in seen_fields:
                raise MappingError(
                    f"entity '{node.name}' declares field '{fname}' "
                    f"more than once")
            seen_fields.add(fname)

            base, is_list, is_nonnull = _type_shape(field.type)
            model = FieldModel(fname, base, is_list, is_nonnull)

            links = _directive(field, "link")
            if len(links) > 1:
                raise MappingError(
                    f"field '{node.name}.{fname}' declares @link more "
                    f"than once")
            if links:
                local = _string_arg(links[0], "local",
                                    f"field '{node.name}.{fname}'")
                target = _string_arg(links[0], "target",
                                     f"field '{node.name}.{fname}'")
                model.link_local = local
                model.link_target = target
            entity.fields[fname] = model

        if key not in entity.fields:
            raise MappingError(
                f"entity '{node.name}': @entity key '{key}' is not a "
                f"declared field")
        mapping.entities[entity.name] = entity
        mapping.tables[table] = entity.name

    # Third pass: resolve links now that every entity is known.
    composite = set()
    for tname, tnode in type_defs.items():
        if isinstance(tnode, (ast.ObjectTypeDefinition,
                              ast.InterfaceTypeDefinition,
                              ast.UnionTypeDefinition)):
            composite.add(tname)
    for entity in mapping.entities.values():
        for field in entity.fields.values():
            if field.is_link:
                target_entity = mapping.entities.get(field.type_name)
                if target_entity is None:
                    # A @link target must be an @entity; whether the name is
                    # undeclared or merely a plain object, it is not mapped.
                    raise UnknownEntity(
                        f"field '{entity.name}.{field.name}' is linked to "
                        f"unmapped type '{field.type_name}'")
                if field.link_target != target_entity.key:
                    raise InvalidJoin(
                        f"@link on '{entity.name}.{field.name}' targets "
                        f"'{target_entity.name}.{field.link_target}', which "
                        f"is not the entity's primary key "
                        f"'{target_entity.key}'")
                continue
            # A field whose base type is another entity must be a @link.
            if field.type_name in mapping.entities:
                raise MappingError(
                    f"field '{entity.name}.{field.name}' has entity type "
                    f"'{field.type_name}' but is missing a @link directive")
            # A composite (object/interface/union) base type without a link
            # cannot be stored as a scalar column.
            if field.type_name in composite:
                raise MappingError(
                    f"field '{entity.name}.{field.name}' has object type "
                    f"'{field.type_name}', which is not a mapped entity")

    # Root types (Query / Mutation / Subscription) describe table scans.
    for kind, type_name in root_names.items():
        root_node = type_defs.get(type_name)
        if root_node is None:
            continue
        if not isinstance(root_node, ast.ObjectTypeDefinition):
            raise MappingError(
                f"root type '{type_name}' must be an object type")
        root = RootTypeModel(type_name, kind)
        for field in root_node.fields:
            base, is_list, is_nonnull = _type_shape(field.type)
            if base not in mapping.entities:
                raise MappingError(
                    f"root field '{type_name}.{field.name.value}' returns "
                    f"unmapped type '{base}'")
            target_entity = mapping.entities[base]
            arg_types = {}
            for arg in field.arguments:
                aname = arg.name.value
                if aname in arg_types:
                    raise MappingError(
                        f"root field '{type_name}.{field.name.value}' "
                        f"declares argument '{aname}' more than once")
                if aname not in target_entity.fields:
                    raise MappingError(
                        f"root field '{type_name}.{field.name.value}' "
                        f"argument '{aname}' does not map to a field of "
                        f"entity '{base}'")
                arg_base, _, _ = _type_shape(arg.type)
                if arg_base in mapping.entities:
                    raise MappingError(
                        f"root field '{type_name}.{field.name.value}' "
                        f"argument '{aname}' has object type '{arg_base}'; "
                        f"only scalar filters are supported")
                arg_types[aname] = arg
            root.fields[field.name.value] = RootFieldModel(
                field.name.value, base, arg_types)
        mapping.root_types[kind] = root

    return mapping
