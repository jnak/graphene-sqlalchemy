from collections import OrderedDict

import sqlalchemy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import (ColumnProperty, CompositeProperty,
                            RelationshipProperty)
from sqlalchemy.orm.exc import NoResultFound

from graphene import Field
from graphene.relay import Connection, Node
from graphene.types.objecttype import ObjectType, ObjectTypeOptions
from graphene.types.utils import yank_fields_from_attrs
from graphene.utils.orderedtype import OrderedType

from .converter import (convert_sqlalchemy_column,
                        convert_sqlalchemy_composite,
                        convert_sqlalchemy_hybrid_method,
                        convert_sqlalchemy_relationship)
from .enums import (enum_for_field, sort_argument_for_object_type,
                    sort_enum_for_object_type)
from .registry import Registry, get_global_registry
from .resolvers import get_attr_resolver, get_custom_resolver
from .utils import get_query, is_mapped_class, is_mapped_instance


class ORMField(OrderedType):
    def __init__(
        self,
        model_attr=None,
        type=None,
        required=None,
        description=None,
        deprecation_reason=None,
        batching=None,
        _creation_counter=None,
        **field_kwargs
    ):
        """
        Use this to override fields automatically generated by SQLAlchemyObjectType.
        Unless specified, options will default to SQLAlchemyObjectType usual behavior
        for the given SQLAlchemy model property.

        Usage:
            class MyModel(Base):
                id = Column(Integer(), primary_key=True)
                name = Column(String)

            class MyType(SQLAlchemyObjectType):
                class Meta:
                    model = MyModel

                id = ORMField(type=graphene.Int)
                name = ORMField(required=True)

        -> MyType.id will be of type Int (vs ID).
        -> MyType.name will be of type NonNull(String) (vs String).

        :param str model_attr:
            Name of the SQLAlchemy model attribute used to resolve this field.
            Default to the name of the attribute referencing the ORMField.
        :param type:
            Default to the type mapping in converter.py.
        :param str description:
            Default to the `doc` attribute of the SQLAlchemy column property.
        :param bool required:
            Default to the opposite of the `nullable` attribute of the SQLAlchemy column property.
        :param str description:
            Same behavior as in graphene.Field. Defaults to None.
        :param str deprecation_reason:
            Same behavior as in graphene.Field. Defaults to None.
        :param bool batching:
            Toggle SQL batching. Defaults to None, that is `SQLAlchemyObjectType.meta.batching`.
        :param int _creation_counter:
            Same behavior as in graphene.Field.
        """
        super(ORMField, self).__init__(_creation_counter=_creation_counter)
        # The is only useful for documentation and auto-completion
        common_kwargs = {
            'model_attr': model_attr,
            'type': type,
            'required': required,
            'description': description,
            'deprecation_reason': deprecation_reason,
            'batching': batching,
        }
        common_kwargs = {kwarg: value for kwarg, value in common_kwargs.items() if value is not None}
        self.kwargs = field_kwargs
        self.kwargs.update(common_kwargs)


def construct_fields(
    obj_type, model, registry, only_fields, exclude_fields, batching, connection_field_factory
):
    """
    Construct all the fields for a SQLAlchemyObjectType.
    The main steps are:
      - Gather all the relevant attributes from the SQLAlchemy model
      - Gather all the ORM fields defined on the type
      - Merge in overrides and build up all the fields

    :param SQLAlchemyObjectType obj_type:
    :param model: the SQLAlchemy model
    :param Registry registry:
    :param tuple[string] only_fields:
    :param tuple[string] exclude_fields:
    :param bool batching:
    :param function|None connection_field_factory:
    :rtype: OrderedDict[str, graphene.Field]
    """
    inspected_model = sqlalchemy.inspect(model)
    # Gather all the relevant attributes from the SQLAlchemy model in order
    all_model_attrs = OrderedDict(
        inspected_model.column_attrs.items() +
        inspected_model.composites.items() +
        [(name, item) for name, item in inspected_model.all_orm_descriptors.items()
            if isinstance(item, hybrid_property)] +
        inspected_model.relationships.items()
    )

    # Filter out excluded fields
    auto_orm_field_names = []
    for attr_name, attr in all_model_attrs.items():
        if (only_fields and attr_name not in only_fields) or (attr_name in exclude_fields):
            continue
        auto_orm_field_names.append(attr_name)

    # Gather all the ORM fields defined on the type
    custom_orm_fields_items = [
        (attn_name, attr)
        for base in reversed(obj_type.__mro__)
        for attn_name, attr in base.__dict__.items()
        if isinstance(attr, ORMField)
    ]
    custom_orm_fields_items = sorted(custom_orm_fields_items, key=lambda item: item[1])

    # Set the model_attr if not set
    for orm_field_name, orm_field in custom_orm_fields_items:
        attr_name = orm_field.kwargs.get('model_attr', orm_field_name)
        if attr_name not in all_model_attrs:
            raise ValueError((
                "Cannot map ORMField to a model attribute.\n"
                "Field: '{}.{}'"
            ).format(obj_type.__name__, orm_field_name,))
        orm_field.kwargs['model_attr'] = attr_name

    # Merge automatic fields with custom ORM fields
    orm_fields = OrderedDict(custom_orm_fields_items)
    for orm_field_name in auto_orm_field_names:
        if orm_field_name in orm_fields:
            continue
        orm_fields[orm_field_name] = ORMField(model_attr=orm_field_name)

    # Build all the field dictionary
    fields = OrderedDict()
    for orm_field_name, orm_field in orm_fields.items():
        attr_name = orm_field.kwargs.pop('model_attr')
        attr = all_model_attrs[attr_name]
        resolver = get_custom_resolver(obj_type, orm_field_name) or get_attr_resolver(obj_type, attr_name)

        if isinstance(attr, ColumnProperty):
            field = convert_sqlalchemy_column(attr, registry, resolver, **orm_field.kwargs)
        elif isinstance(attr, RelationshipProperty):
            batching_ = orm_field.kwargs.pop('batching', batching)
            field = convert_sqlalchemy_relationship(
                attr, obj_type, connection_field_factory, batching_, orm_field_name, **orm_field.kwargs)
        elif isinstance(attr, CompositeProperty):
            if attr_name != orm_field_name or orm_field.kwargs:
                # TODO Add a way to override composite property fields
                raise ValueError(
                    "ORMField kwargs for composite fields must be empty. "
                    "Field: {}.{}".format(obj_type.__name__, orm_field_name))
            field = convert_sqlalchemy_composite(attr, registry, resolver)
        elif isinstance(attr, hybrid_property):
            field = convert_sqlalchemy_hybrid_method(attr, resolver, **orm_field.kwargs)
        else:
            raise Exception('Property type is not supported')  # Should never happen

        registry.register_orm_field(obj_type, orm_field_name, attr)
        fields[orm_field_name] = field

    return fields


class SQLAlchemyObjectTypeOptions(ObjectTypeOptions):
    model = None  # type: sqlalchemy.Model
    registry = None  # type: sqlalchemy.Registry
    connection = None  # type: sqlalchemy.Type[sqlalchemy.Connection]
    id = None  # type: str


class SQLAlchemyObjectType(ObjectType):
    @classmethod
    def __init_subclass_with_meta__(
        cls,
        model=None,
        registry=None,
        skip_registry=False,
        only_fields=(),
        exclude_fields=(),
        connection=None,
        connection_class=None,
        use_connection=None,
        interfaces=(),
        id=None,
        batching=False,
        connection_field_factory=None,
        _meta=None,
        **options
    ):
        assert is_mapped_class(model), (
            "You need to pass a valid SQLAlchemy Model in " '{}.Meta, received "{}".'
        ).format(cls.__name__, model)

        if not registry:
            registry = get_global_registry()

        assert isinstance(registry, Registry), (
            "The attribute registry in {} needs to be an instance of "
            'Registry, received "{}".'
        ).format(cls.__name__, registry)

        if only_fields and exclude_fields:
            raise ValueError("The options 'only_fields' and 'exclude_fields' cannot be both set on the same type.")

        sqla_fields = yank_fields_from_attrs(
            construct_fields(
                obj_type=cls,
                model=model,
                registry=registry,
                only_fields=only_fields,
                exclude_fields=exclude_fields,
                batching=batching,
                connection_field_factory=connection_field_factory,
            ),
            _as=Field,
            sort=False,
        )

        if use_connection is None and interfaces:
            use_connection = any(
                (issubclass(interface, Node) for interface in interfaces)
            )

        if use_connection and not connection:
            # We create the connection automatically
            if not connection_class:
                connection_class = Connection

            connection = connection_class.create_type(
                "{}Connection".format(cls.__name__), node=cls
            )

        if connection is not None:
            assert issubclass(connection, Connection), (
                "The connection must be a Connection. Received {}"
            ).format(connection.__name__)

        if not _meta:
            _meta = SQLAlchemyObjectTypeOptions(cls)

        _meta.model = model
        _meta.registry = registry

        if _meta.fields:
            _meta.fields.update(sqla_fields)
        else:
            _meta.fields = sqla_fields

        _meta.connection = connection
        _meta.id = id or "id"

        super(SQLAlchemyObjectType, cls).__init_subclass_with_meta__(
            _meta=_meta, interfaces=interfaces, **options
        )

        if not skip_registry:
            registry.register(cls)

    @classmethod
    def is_type_of(cls, root, info):
        if isinstance(root, cls):
            return True
        if not is_mapped_instance(root):
            raise Exception(('Received incompatible instance "{}".').format(root))
        return isinstance(root, cls._meta.model)

    @classmethod
    def get_query(cls, info):
        model = cls._meta.model
        return get_query(model, info.context)

    @classmethod
    def get_node(cls, info, id):
        try:
            return cls.get_query(info).get(id)
        except NoResultFound:
            return None

    def resolve_id(self, info):
        # graphene_type = info.parent_type.graphene_type
        keys = self.__mapper__.primary_key_from_instance(self)
        return tuple(keys) if len(keys) > 1 else keys[0]

    @classmethod
    def enum_for_field(cls, field_name):
        return enum_for_field(cls, field_name)

    sort_enum = classmethod(sort_enum_for_object_type)

    sort_argument = classmethod(sort_argument_for_object_type)
