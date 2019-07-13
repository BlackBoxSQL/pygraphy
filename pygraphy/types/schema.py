import json
import traceback
import contextvars
from graphql.language import parse
from graphql.language.ast import (
    OperationDefinitionNode,
    OperationType
)
from pygraphy.utils import (
    is_union,
    is_list,
    is_optional,
    patch_indents
)
from pygraphy.encoder import GraphQLEncoder
from pygraphy.exceptions import ValidationError
from pygraphy.context import Context
from .object import ObjectType, Object
from .field import Field, ResolverField
from .union import UnionType
from .input import InputType
from .interface import InterfaceType
from .enum import EnumType


class SchemaType(ObjectType):
    VALID_ROOT_TYPES = {'query', 'mutation'}

    def __new__(cls, name, bases, attrs):
        attrs['registered_type'] = []
        without_dataclass = type.__new__(cls, name, bases, attrs)

        cls = super().__new__(cls, name, bases, attrs)
        cls.validated_type = []
        cls.validate()
        cls.register_fields_type(cls.__fields__.values())

        # Schema does not need dataclass
        without_dataclass.__fields__ = cls.__fields__
        without_dataclass.__description__ = cls.__description__
        return without_dataclass

    def register_fields_type(cls, fields):
        param_return_types = []
        for field in fields:
            param_return_types.append(field.ftype)
            if isinstance(field, ResolverField):
                param_return_types.extend(field.params.values())
        cls.register_types(param_return_types)

    def register_types(cls, types):
        for ptype in types:
            if ptype in cls.validated_type:
                continue
            cls.validated_type.append(ptype)

            if isinstance(ptype, ObjectType):
                cls.registered_type.append(ptype)
                cls.register_fields_type(ptype.__fields__.values())
            elif is_union(ptype) or is_list(ptype):
                cls.register_types(ptype.__args__)
            elif isinstance(ptype, UnionType):
                cls.registered_type.append(ptype)
                cls.register_types(ptype.members)
            elif isinstance(ptype, InputType):
                cls.registered_type.append(ptype)
                cls.register_fields_type(ptype.__fields__.values())
            elif isinstance(ptype, InterfaceType):
                cls.registered_type.append(ptype)
                cls.register_fields_type(ptype.__fields__.values())
                cls.register_types(ptype.__subclasses__())
            elif isinstance(ptype, EnumType):
                cls.registered_type.append(ptype)
            else:
                # Other basic types, do not need be handled
                pass

    def validate(cls):
        for name, field in cls.__fields__.items():
            if name not in cls.VALID_ROOT_TYPES:
                raise ValidationError(
                    f'The valid root type must be {cls.VALID_ROOT_TYPES},'
                    f' rather than {name}'
                )
            if not isinstance(field, Field):
                raise ValidationError(f'{field} is an invalid field type')
            if not is_optional(field.ftype):
                raise ValidationError(
                    f'The return type of root object should be Optional'
                )
            if not isinstance(field.ftype.__args__[0], ObjectType):
                raise ValidationError(
                    f'The typt of root object must be an Object, rather than {field.ftype}'
                )
        ObjectType.validate(cls)

    def __str__(cls):
        string = ''
        for rtype in cls.registered_type:
            string += (str(rtype) + '\n\n')
        schema = (
            f'{cls.print_description()}'
            + f'schema '
            + '{\n'
            + f'{patch_indents(cls.print_field(), indent=1)}'
            + '\n}'
        )
        return string + schema


context: contextvars.ContextVar[Context] = contextvars.ContextVar('context')


class Schema(Object, metaclass=SchemaType):

    FIELD_MAP = {
        OperationType.QUERY: 'query',
        OperationType.MUTATION: 'mutation'
    }

    @classmethod
    async def execute(cls, query, variables=None, request=None):
        document = parse(query)
        for definition in document.definitions:
            if not isinstance(definition, OperationDefinitionNode):
                continue
            if definition.operation in (
                OperationType.QUERY,
                OperationType.MUTATION
            ):
                obj = cls.__fields__[
                    cls.FIELD_MAP[definition.operation]
                ].ftype.__args__[0]()
                error_collector = []
                token = context.set(
                    Context(
                        schema=cls,
                        root_ast=document.definitions,
                        request=request,
                        variables=variables
                    )
                )
                try:
                    obj = await obj.__resolve__(
                        definition.selection_set.selections,
                        error_collector
                    )
                except Exception as e:
                    traceback.print_exc()
                    error_collector.append(e)
                context.reset(token)
                return_root = {
                    'errors': error_collector if error_collector else None,
                    'data': obj
                }
                success = True if not error_collector else False
                return json.dumps(return_root, cls=GraphQLEncoder), success