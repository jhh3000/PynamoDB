"""
DynamoDB Models for PynamoDB
"""
import json
import random
import time
import logging
import warnings
from inspect import getmembers
from typing import Any, Dict, Generic, Iterable, Iterator, List, Optional, Sequence, Mapping, Type, TypeVar, Text, \
    Tuple, Union, cast

from pynamodb.expressions.update import Action
from pynamodb.exceptions import DoesNotExist, TableDoesNotExist, TableError, InvalidStateError, PutError
from pynamodb.attributes import (
    Attribute, AttributeContainer, AttributeContainerMeta, MapAttribute, TTLAttribute, VersionAttribute
)
from pynamodb.connection.table import TableConnection
from pynamodb.expressions.condition import Condition
from pynamodb.types import HASH, RANGE
from pynamodb.indexes import Index, GlobalSecondaryIndex
from pynamodb.pagination import ResultIterator
from pynamodb.settings import get_settings_value
from pynamodb.constants import (
    ATTR_TYPE_MAP, ATTR_DEFINITIONS, ATTR_NAME, ATTR_TYPE, KEY_SCHEMA,
    KEY_TYPE, ITEM, READ_CAPACITY_UNITS, WRITE_CAPACITY_UNITS,
    RANGE_KEY, ATTRIBUTES, PUT, DELETE, RESPONSES,
    INDEX_NAME, PROVISIONED_THROUGHPUT, PROJECTION, ALL_NEW,
    GLOBAL_SECONDARY_INDEXES, LOCAL_SECONDARY_INDEXES, KEYS,
    PROJECTION_TYPE, NON_KEY_ATTRIBUTES,
    TABLE_STATUS, ACTIVE, RETURN_VALUES, BATCH_GET_PAGE_LIMIT,
    UNPROCESSED_KEYS, PUT_REQUEST, DELETE_REQUEST,
    BATCH_WRITE_PAGE_LIMIT,
    META_CLASS_NAME, REGION, HOST, NULL,
    COUNT, ITEM_COUNT, KEY, UNPROCESSED_ITEMS, STREAM_VIEW_TYPE,
    STREAM_SPECIFICATION, STREAM_ENABLED, BILLING_MODE, PAY_PER_REQUEST_BILLING_MODE
)
from pynamodb.util import snake_to_camel_case

_T = TypeVar('_T', bound='Model')
_KeyType = Any


log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class ModelContextManager(Generic[_T]):
    """
    A class for managing batch operations
    """

    def __init__(self, model: Type[_T], auto_commit: bool = True):
        self.model = model
        self.auto_commit = auto_commit
        self.max_operations = BATCH_WRITE_PAGE_LIMIT
        self.pending_operations: List[Dict[str, Any]] = []
        self.failed_operations: List[Any] = []

    def __enter__(self):
        return self


class BatchWrite(ModelContextManager, Generic[_T]):
    """
    A class for batch writes
    """
    def save(self, put_item: _T) -> None:
        """
        This adds `put_item` to the list of pending operations to be performed.

        If the list currently contains 25 items, which is the DynamoDB imposed
        limit on a BatchWriteItem call, one of two things will happen. If auto_commit
        is True, a BatchWriteItem operation will be sent with the already pending
        writes after which put_item is appended to the (now empty) list. If auto_commit
        is False, ValueError is raised to indicate additional items cannot be accepted
        due to the DynamoDB imposed limit.

        :param put_item: Should be an instance of a `Model` to be written
        """
        if len(self.pending_operations) == self.max_operations:
            if not self.auto_commit:
                raise ValueError("DynamoDB allows a maximum of 25 batch operations")
            else:
                self.commit()
        self.pending_operations.append({"action": PUT, "item": put_item})

    def delete(self, del_item: _T) -> None:
        """
        This adds `del_item` to the list of pending operations to be performed.

        If the list currently contains 25 items, which is the DynamoDB imposed
        limit on a BatchWriteItem call, one of two things will happen. If auto_commit
        is True, a BatchWriteItem operation will be sent with the already pending
        operations after which put_item is appended to the (now empty) list. If auto_commit
        is False, ValueError is raised to indicate additional items cannot be accepted
        due to the DynamoDB imposed limit.

        :param del_item: Should be an instance of a `Model` to be deleted
        """
        if len(self.pending_operations) == self.max_operations:
            if not self.auto_commit:
                raise ValueError("DynamoDB allows a maximum of 25 batch operations")
            else:
                self.commit()
        self.pending_operations.append({"action": DELETE, "item": del_item})

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        This ensures that all pending operations are committed when
        the context is exited
        """
        return self.commit()

    def commit(self) -> None:
        """
        Writes all of the changes that are pending
        """
        log.debug("%s committing batch operation", self.model)
        put_items = []
        delete_items = []
        attrs_name = snake_to_camel_case(ATTRIBUTES)
        for item in self.pending_operations:
            if item['action'] == PUT:
                put_items.append(item['item']._serialize(attr_map=True)[attrs_name])
            elif item['action'] == DELETE:
                delete_items.append(item['item']._get_keys())
        self.pending_operations = []
        if not len(put_items) and not len(delete_items):
            return
        data = self.model._get_connection().batch_write_item(
            put_items=put_items,
            delete_items=delete_items
        )
        if data is None:
            return
        retries = 0
        unprocessed_items = data.get(UNPROCESSED_ITEMS, {}).get(self.model.Meta.table_name)
        while unprocessed_items:
            sleep_time = random.randint(0, self.model.Meta.base_backoff_ms * (2 ** retries)) / 1000
            time.sleep(sleep_time)
            retries += 1
            if retries >= self.model.Meta.max_retry_attempts:
                self.failed_operations = unprocessed_items
                raise PutError("Failed to batch write items: max_retry_attempts exceeded")
            put_items = []
            delete_items = []
            for item in unprocessed_items:
                if PUT_REQUEST in item:
                    put_items.append(item.get(PUT_REQUEST).get(ITEM))  # type: ignore
                elif DELETE_REQUEST in item:
                    delete_items.append(item.get(DELETE_REQUEST).get(KEY))  # type: ignore
            log.info("Resending %d unprocessed keys for batch operation after %d seconds sleep",
                     len(unprocessed_items), sleep_time)
            data = self.model._get_connection().batch_write_item(
                put_items=put_items,
                delete_items=delete_items
            )
            unprocessed_items = data.get(UNPROCESSED_ITEMS, {}).get(self.model.Meta.table_name)


class DefaultMeta(object):
    pass


# TODO(garrettheel): is this used anywhere?
class ResultSet(Iterable):

    def __init__(self, results, operation, arguments):
        self.results = results
        self.operation = operation
        self.arguments = arguments

    def __iter__(self):
        return iter(self.results)


class MetaModel(AttributeContainerMeta):
    table_name: str
    read_capacity_units: Optional[int]
    write_capacity_units: Optional[int]
    region: Optional[str]
    host: Optional[str]
    connect_timeout_seconds: int
    read_timeout_seconds: int
    base_backoff_ms: int
    max_retry_attempts: int
    max_pool_connections: int
    extra_headers: Mapping[str, str]
    aws_access_key_id: Optional[str]
    aws_secret_access_key: Optional[str]
    aws_session_token: Optional[str]
    billing_mode: Optional[str]
    stream_view_type: Optional[str]

    """
    Model meta class

    This class is just here so that index queries have nice syntax.
    Model.index.query()
    """
    def __init__(self, name: str, bases: Any, attrs: Dict[str, Any]) -> None:
        super().__init__(name, bases, attrs)
        cls = cast(Type['Model'], self)
        for attr_name, attribute in cls.get_attributes().items():
            if attribute.is_hash_key:
                cls._hash_keyname = attr_name
            if attribute.is_range_key:
                cls._range_keyname = attr_name
            if isinstance(attribute, VersionAttribute):
                if cls._version_attribute_name:
                    raise ValueError(
                        "The model has more than one Version attribute: {}, {}"
                        .format(cls._version_attribute_name, attr_name)
                    )
                cls._version_attribute_name = attr_name

        if isinstance(attrs, dict):
            for attr_name, attr_obj in attrs.items():
                if attr_name == META_CLASS_NAME:
                    if not hasattr(attr_obj, REGION):
                        setattr(attr_obj, REGION, get_settings_value('region'))
                    if not hasattr(attr_obj, HOST):
                        setattr(attr_obj, HOST, get_settings_value('host'))
                    if hasattr(attr_obj, 'session_cls') or hasattr(attr_obj, 'request_timeout_seconds'):
                        warnings.warn("The `session_cls` and `request_timeout_second` options are no longer supported")
                    if not hasattr(attr_obj, 'connect_timeout_seconds'):
                        setattr(attr_obj, 'connect_timeout_seconds', get_settings_value('connect_timeout_seconds'))
                    if not hasattr(attr_obj, 'read_timeout_seconds'):
                        setattr(attr_obj, 'read_timeout_seconds', get_settings_value('read_timeout_seconds'))
                    if not hasattr(attr_obj, 'base_backoff_ms'):
                        setattr(attr_obj, 'base_backoff_ms', get_settings_value('base_backoff_ms'))
                    if not hasattr(attr_obj, 'max_retry_attempts'):
                        setattr(attr_obj, 'max_retry_attempts', get_settings_value('max_retry_attempts'))
                    if not hasattr(attr_obj, 'max_pool_connections'):
                        setattr(attr_obj, 'max_pool_connections', get_settings_value('max_pool_connections'))
                    if not hasattr(attr_obj, 'extra_headers'):
                        setattr(attr_obj, 'extra_headers', get_settings_value('extra_headers'))
                    if not hasattr(attr_obj, 'aws_access_key_id'):
                        setattr(attr_obj, 'aws_access_key_id', None)
                    if not hasattr(attr_obj, 'aws_secret_access_key'):
                        setattr(attr_obj, 'aws_secret_access_key', None)
                    if not hasattr(attr_obj, 'aws_session_token'):
                        setattr(attr_obj, 'aws_session_token', None)
                elif isinstance(attr_obj, Index):
                    attr_obj._model = cls
                    if not hasattr(attr_obj.Meta, "index_name"):
                        attr_obj.Meta.index_name = attr_name
                elif isinstance(attr_obj, Attribute):
                    if attr_obj.attr_name is None:
                        attr_obj.attr_name = attr_name

            ttl_attr_names = [name for name, attr_obj in attrs.items() if isinstance(attr_obj, TTLAttribute)]
            if len(ttl_attr_names) > 1:
                raise ValueError("The model has more than one TTL attribute: {}".format(", ".join(ttl_attr_names)))

            if META_CLASS_NAME not in attrs:
                setattr(cls, META_CLASS_NAME, DefaultMeta)

            # create a custom Model.DoesNotExist derived from pynamodb.exceptions.DoesNotExist,
            # so that "except Model.DoesNotExist:" would not catch other models' exceptions
            if 'DoesNotExist' not in attrs:
                exception_attrs = {
                    '__module__': attrs.get('__module__'),
                    '__qualname__': f'{cls.__qualname__}.{"DoesNotExist"}',
                }
                cls.DoesNotExist = type('DoesNotExist', (DoesNotExist, ), exception_attrs)


class Model(AttributeContainer, metaclass=MetaModel):
    """
    Defines a `PynamoDB` Model

    This model is backed by a table in DynamoDB.
    You can create the table by with the ``create_table`` method.
    """

    # These attributes are named to avoid colliding with user defined
    # DynamoDB attributes
    _hash_keyname: Optional[str] = None
    _range_keyname: Optional[str] = None
    _indexes: Optional[Dict[str, List[Any]]] = None
    _connection: Optional[TableConnection] = None
    _index_classes: Optional[Dict[str, Any]] = None
    DoesNotExist: Type[DoesNotExist] = DoesNotExist
    _version_attribute_name: Optional[str] = None

    Meta: MetaModel

    def __init__(
        self,
        hash_key: Optional[_KeyType] = None,
        range_key: Optional[_KeyType] = None,
        _user_instantiated: bool = True,
        **attributes: Any,
    ) -> None:
        """
        :param hash_key: Required. The hash key for this object.
        :param range_key: Only required if the table has a range key attribute.
        :param attrs: A dictionary of attributes to set on this object.
        """
        if hash_key is not None:
            if self._hash_keyname is None:
                raise ValueError(f"This model has no hash key, but a hash key value was provided: {range_key}")
            attributes[self._hash_keyname] = hash_key
        if range_key is not None:
            if self._range_keyname is None:
                raise ValueError(f"This model has no range key, but a range key value was provided: {range_key}")
            attributes[self._range_keyname] = range_key
        super(Model, self).__init__(_user_instantiated=_user_instantiated, **attributes)

    @classmethod
    def batch_get(
        cls: Type[_T],
        items: Iterable[Union[_KeyType, Iterable[_KeyType]]],
        consistent_read: Optional[bool] = None,
        attributes_to_get: Optional[Sequence[str]] = None,
    ) -> Iterator[_T]:
        """
        BatchGetItem for this model

        :param items: Should be a list of hash keys to retrieve, or a list of
            tuples if range keys are used.
        """
        items = list(items)
        hash_key_attribute = cls._hash_key_attribute()
        range_key_attribute = cls._range_key_attribute()
        keys_to_get: List[Any] = []
        while items:
            if len(keys_to_get) == BATCH_GET_PAGE_LIMIT:
                while keys_to_get:
                    page, unprocessed_keys = cls._batch_get_page(
                        keys_to_get,
                        consistent_read=consistent_read,
                        attributes_to_get=attributes_to_get
                    )
                    for batch_item in page:
                        yield cls.from_raw_data(batch_item)
                    if unprocessed_keys:
                        keys_to_get = unprocessed_keys
                    else:
                        keys_to_get = []
            item = items.pop()
            if range_key_attribute:
                hash_key, range_key = cls._serialize_keys(item[0], item[1])  # type: ignore
                keys_to_get.append({
                    hash_key_attribute.attr_name: hash_key,
                    range_key_attribute.attr_name: range_key
                })
            else:
                hash_key = cls._serialize_keys(item)[0]
                keys_to_get.append({
                    hash_key_attribute.attr_name: hash_key
                })

        while keys_to_get:
            page, unprocessed_keys = cls._batch_get_page(
                keys_to_get,
                consistent_read=consistent_read,
                attributes_to_get=attributes_to_get
            )
            for batch_item in page:
                yield cls.from_raw_data(batch_item)
            if unprocessed_keys:
                keys_to_get = unprocessed_keys
            else:
                keys_to_get = []

    @classmethod
    def batch_write(cls: Type[_T], auto_commit: bool = True) -> BatchWrite[_T]:
        """
        Returns a BatchWrite context manager for a batch operation.

        :param auto_commit: If true, the context manager will commit writes incrementally
                            as items are written to as necessary to honor item count limits
                            in the DynamoDB API (see BatchWrite). Regardless of the value
                            passed here, changes automatically commit on context exit
                            (whether successful or not).
        """
        return BatchWrite(cls, auto_commit=auto_commit)

    def __repr__(self) -> str:
        table_name = self.Meta.table_name if self.Meta.table_name else 'unknown'
        serialized = self._serialize(null_check=False)
        if self._range_keyname:
            msg = "{}<{}, {}>".format(self.Meta.table_name, serialized.get(HASH), serialized.get(RANGE))
        else:
            msg = "{}<{}>".format(self.Meta.table_name, serialized.get(HASH))
        return msg

    def delete(self, condition: Optional[Condition] = None) -> Any:
        """
        Deletes this object from dynamodb

        :raises pynamodb.exceptions.DeleteError: If the record can not be deleted
        """
        args, kwargs = self._get_save_args(attributes=False, null_check=False)
        version_condition = self._handle_version_attribute(kwargs)
        if version_condition is not None:
            condition &= version_condition

        kwargs.update(condition=condition)
        return self._get_connection().delete_item(*args, **kwargs)

    def update(self, actions: Sequence[Action], condition: Optional[Condition] = None) -> Any:
        """
        Updates an item using the UpdateItem operation.

        :param actions: a list of Action updates to apply
        :param condition: an optional Condition on which to update
        :raises ModelInstance.DoesNotExist: if the object to be updated does not exist
        :raises pynamodb.exceptions.UpdateError: if the `condition` is not met
        """
        if not isinstance(actions, list) or len(actions) == 0:
            raise TypeError("the value of `actions` is expected to be a non-empty list")

        args, save_kwargs = self._get_save_args(null_check=False)
        version_condition = self._handle_version_attribute(save_kwargs, actions=actions)
        if version_condition is not None:
            condition &= version_condition
        kwargs: Dict[str, Any] = {
            snake_to_camel_case(RETURN_VALUES):  ALL_NEW,
        }

        if snake_to_camel_case(RANGE_KEY) in save_kwargs:
            kwargs[snake_to_camel_case(RANGE_KEY)] = save_kwargs[snake_to_camel_case(RANGE_KEY)]

        kwargs.update(condition=condition)
        kwargs.update(actions=actions)

        data = self._get_connection().update_item(*args, **kwargs)
        self._deserialize(data[ATTRIBUTES])
        return data

    def save(self, condition: Optional[Condition] = None) -> Dict[str, Any]:
        """
        Save this object to dynamodb
        """
        args, kwargs = self._get_save_args()
        version_condition = self._handle_version_attribute(serialized_attributes=kwargs)
        if version_condition is not None:
            condition &= version_condition
        kwargs.update(condition=condition)
        data = self._get_connection().put_item(*args, **kwargs)
        self.update_local_version_attribute()
        return data

    def refresh(self, consistent_read: bool = False) -> None:
        """
        Retrieves this object's data from dynamodb and syncs this local object

        :param consistent_read: If True, then a consistent read is performed.
        :raises ModelInstance.DoesNotExist: if the object to be updated does not exist
        """
        args, kwargs = self._get_save_args(attributes=False, null_check=False)
        kwargs.setdefault('consistent_read', consistent_read)
        attrs = self._get_connection().get_item(*args, **kwargs)
        item_data = attrs.get(ITEM, None)
        if item_data is None:
            raise self.DoesNotExist("This item does not exist in the table.")
        self._deserialize(item_data)

    def get_operation_kwargs_from_instance(
        self,
        key: str = KEY,
        actions: Optional[Sequence[Action]] = None,
        condition: Optional[Condition] = None,
        return_values_on_condition_failure: Optional[str] = None,
    ) -> Dict[str, Any]:
        is_update = actions is not None
        is_delete = actions is None and key is KEY
        args, save_kwargs = self._get_save_args(null_check=not is_update)

        version_condition = self._handle_version_attribute(
            serialized_attributes={} if is_delete else save_kwargs,
            actions=actions
        )
        if version_condition is not None:
            condition &= version_condition

        kwargs: Dict[str, Any] = dict(
            key=key,
            actions=actions,
            condition=condition,
            return_values_on_condition_failure=return_values_on_condition_failure
        )
        if not is_update:
            kwargs.update(save_kwargs)
        elif snake_to_camel_case(RANGE_KEY) in save_kwargs:
            kwargs[snake_to_camel_case(RANGE_KEY)] = save_kwargs[snake_to_camel_case(RANGE_KEY)]
        return self._get_connection().get_operation_kwargs(*args, **kwargs)

    @classmethod
    def get_operation_kwargs_from_class(
        cls,
        hash_key: str,
        range_key: Optional[_KeyType] = None,
        condition: Optional[Condition] = None,
    ) -> Dict[str, Any]:
        hash_key, range_key = cls._serialize_keys(hash_key, range_key)
        return cls._get_connection().get_operation_kwargs(
            hash_key=hash_key,
            range_key=range_key,
            condition=condition
        )

    @classmethod
    def get(
        cls: Type[_T],
        hash_key: _KeyType,
        range_key: Optional[_KeyType] = None,
        consistent_read: bool = False,
        attributes_to_get: Optional[Sequence[Text]] = None,
    ) -> _T:
        """
        Returns a single object using the provided keys

        :param hash_key: The hash key of the desired item
        :param range_key: The range key of the desired item, only used when appropriate.
        :param consistent_read:
        :param attributes_to_get:
        :raises ModelInstance.DoesNotExist: if the object to be updated does not exist
        """
        hash_key, range_key = cls._serialize_keys(hash_key, range_key)

        data = cls._get_connection().get_item(
            hash_key,
            range_key=range_key,
            consistent_read=consistent_read,
            attributes_to_get=attributes_to_get
        )
        if data:
            item_data = data.get(ITEM)
            if item_data:
                return cls.from_raw_data(item_data)
        raise cls.DoesNotExist()

    @classmethod
    def from_raw_data(cls: Type[_T], data: Dict[str, Any]) -> _T:
        """
        Returns an instance of this class
        from the raw data

        :param data: A serialized DynamoDB object
        """
        if data is None:
            raise ValueError("Received no data to construct object")

        attributes: Dict[str, Any] = {}
        for name, value in data.items():
            attr_name = cls._dynamo_to_python_attr(name)
            attr = cls.get_attributes().get(attr_name, None)  # type: ignore
            if attr:
                attributes[attr_name] = attr.deserialize(attr.get_value(value))  # type: ignore
        return cls(_user_instantiated=False, **attributes)

    @classmethod
    def count(
        cls: Type[_T],
        hash_key: Optional[_KeyType] = None,
        range_key_condition: Optional[Condition] = None,
        filter_condition: Optional[Condition] = None,
        consistent_read: bool = False,
        index_name: Optional[str] = None,
        limit: Optional[int] = None,
        rate_limit: Optional[float] = None,
    ) -> int:
        """
        Provides a filtered count

        :param hash_key: The hash key to query. Can be None.
        :param range_key_condition: Condition for range key
        :param filter_condition: Condition used to restrict the query results
        :param consistent_read: If True, a consistent read is performed
        :param index_name: If set, then this index is used
        :param rate_limit: If set then consumed capacity will be limited to this amount per second
        """
        if hash_key is None:
            if filter_condition is not None:
                raise ValueError('A hash_key must be given to use filters')
            return cls.describe_table().get(ITEM_COUNT)

        cls._get_indexes()
        if cls._index_classes and index_name:
            hash_key = cls._index_classes[index_name]._hash_key_attribute().serialize(hash_key)
        else:
            hash_key = cls._serialize_keys(hash_key)[0]

        query_args = (hash_key,)
        query_kwargs = dict(
            range_key_condition=range_key_condition,
            filter_condition=filter_condition,
            index_name=index_name,
            consistent_read=consistent_read,
            limit=limit,
            select=COUNT
        )

        result_iterator: ResultIterator[_T] = ResultIterator(
            cls._get_connection().query,
            query_args,
            query_kwargs,
            limit=limit,
            rate_limit=rate_limit
        )

        # iterate through results
        list(result_iterator)

        return result_iterator.total_count

    @classmethod
    def query(
        cls: Type[_T],
        hash_key: _KeyType,
        range_key_condition: Optional[Condition] = None,
        filter_condition: Optional[Condition] = None,
        consistent_read: bool = False,
        index_name: Optional[str] = None,
        scan_index_forward: Optional[bool] = None,
        limit: Optional[int] = None,
        last_evaluated_key: Optional[Dict[str, Dict[str, Any]]] = None,
        attributes_to_get: Optional[Iterable[str]] = None,
        page_size: Optional[int] = None,
        rate_limit: Optional[float] = None,
    ) -> ResultIterator[_T]:
        """
        Provides a high level query API

        :param hash_key: The hash key to query
        :param range_key_condition: Condition for range key
        :param filter_condition: Condition used to restrict the query results
        :param consistent_read: If True, a consistent read is performed
        :param index_name: If set, then this index is used
        :param limit: Used to limit the number of results returned
        :param scan_index_forward: If set, then used to specify the same parameter to the DynamoDB API.
            Controls descending or ascending results
        :param last_evaluated_key: If set, provides the starting point for query.
        :param attributes_to_get: If set, only returns these elements
        :param page_size: Page size of the query to DynamoDB
        :param rate_limit: If set then consumed capacity will be limited to this amount per second
        """
        cls._get_indexes()
        if index_name and cls._index_classes:
            hash_key = cls._index_classes[index_name]._hash_key_attribute().serialize(hash_key)
        else:
            hash_key = cls._serialize_keys(hash_key)[0]

        if page_size is None:
            page_size = limit

        query_args = (hash_key,)
        query_kwargs = dict(
            range_key_condition=range_key_condition,
            filter_condition=filter_condition,
            index_name=index_name,
            exclusive_start_key=last_evaluated_key,
            consistent_read=consistent_read,
            scan_index_forward=scan_index_forward,
            limit=page_size,
            attributes_to_get=attributes_to_get,
        )

        return ResultIterator(
            cls._get_connection().query,
            query_args,
            query_kwargs,
            map_fn=cls.from_raw_data,
            limit=limit,
            rate_limit=rate_limit
        )

    @classmethod
    def scan(
        cls: Type[_T],
        filter_condition: Optional[Condition] = None,
        segment: Optional[int] = None,
        total_segments: Optional[int] = None,
        limit: Optional[int] = None,
        last_evaluated_key: Optional[Dict[str, Dict[str, Any]]] = None,
        page_size: Optional[int] = None,
        consistent_read: Optional[bool] = None,
        index_name: Optional[str] = None,
        rate_limit: Optional[float] = None,
        attributes_to_get: Optional[Sequence[str]] = None,
    ) -> ResultIterator[_T]:
        """
        Iterates through all items in the table

        :param filter_condition: Condition used to restrict the scan results
        :param segment: If set, then scans the segment
        :param total_segments: If set, then specifies total segments
        :param limit: Used to limit the number of results returned
        :param last_evaluated_key: If set, provides the starting point for scan.
        :param page_size: Page size of the scan to DynamoDB
        :param consistent_read: If True, a consistent read is performed
        :param index_name: If set, then this index is used
        :param rate_limit: If set then consumed capacity will be limited to this amount per second
        :param attributes_to_get: If set, specifies the properties to include in the projection expression
        """
        if page_size is None:
            page_size = limit

        scan_args = ()
        scan_kwargs = dict(
            filter_condition=filter_condition,
            exclusive_start_key=last_evaluated_key,
            segment=segment,
            limit=page_size,
            total_segments=total_segments,
            consistent_read=consistent_read,
            index_name=index_name,
            attributes_to_get=attributes_to_get
        )

        return ResultIterator(
            cls._get_connection().scan,
            scan_args,
            scan_kwargs,
            map_fn=cls.from_raw_data,
            limit=limit,
            rate_limit=rate_limit,
        )

    @classmethod
    def exists(cls: Type[_T]) -> bool:
        """
        Returns True if this table exists, False otherwise
        """
        try:
            cls._get_connection().describe_table()
            return True
        except TableDoesNotExist:
            return False

    @classmethod
    def delete_table(cls) -> Any:
        """
        Delete the table for this model
        """
        return cls._get_connection().delete_table()

    @classmethod
    def describe_table(cls) -> Any:
        """
        Returns the result of a DescribeTable operation on this model's table
        """
        return cls._get_connection().describe_table()

    @classmethod
    def create_table(
        cls,
        wait: bool = False,
        read_capacity_units: Optional[int] = None,
        write_capacity_units: Optional[int] = None,
        billing_mode: Optional[str] = None,
        ignore_update_ttl_errors: bool = False,
    ) -> Any:
        """
        Create the table for this model

        :param wait: If set, then this call will block until the table is ready for use
        :param read_capacity_units: Sets the read capacity units for this table
        :param write_capacity_units: Sets the write capacity units for this table
        :param billing_mode: Sets the billing mode provisioned (default) or on_demand for this table
        """
        if not cls.exists():
            schema = cls._get_schema()
            if hasattr(cls.Meta, snake_to_camel_case(READ_CAPACITY_UNITS)):
                schema[snake_to_camel_case(READ_CAPACITY_UNITS)] = cls.Meta.read_capacity_units
            if hasattr(cls.Meta, snake_to_camel_case(WRITE_CAPACITY_UNITS)):
                schema[snake_to_camel_case(WRITE_CAPACITY_UNITS)] = cls.Meta.write_capacity_units
            if hasattr(cls.Meta, snake_to_camel_case(STREAM_VIEW_TYPE)):
                schema[snake_to_camel_case(STREAM_SPECIFICATION)] = {
                    snake_to_camel_case(STREAM_ENABLED): True,
                    snake_to_camel_case(STREAM_VIEW_TYPE): cls.Meta.stream_view_type
                }
            if hasattr(cls.Meta, snake_to_camel_case(BILLING_MODE)):
                schema[snake_to_camel_case(BILLING_MODE)] = cls.Meta.billing_mode
            if read_capacity_units is not None:
                schema[snake_to_camel_case(READ_CAPACITY_UNITS)] = read_capacity_units
            if write_capacity_units is not None:
                schema[snake_to_camel_case(WRITE_CAPACITY_UNITS)] = write_capacity_units
            if billing_mode is not None:
                schema[snake_to_camel_case(BILLING_MODE)] = billing_mode
            index_data = cls._get_indexes()
            schema[snake_to_camel_case(GLOBAL_SECONDARY_INDEXES)] = index_data.get(snake_to_camel_case(GLOBAL_SECONDARY_INDEXES))
            schema[snake_to_camel_case(LOCAL_SECONDARY_INDEXES)] = index_data.get(snake_to_camel_case(LOCAL_SECONDARY_INDEXES))
            index_attrs = index_data.get(snake_to_camel_case(ATTR_DEFINITIONS))
            attr_keys = [attr.get(snake_to_camel_case(ATTR_NAME)) for attr in schema.get(snake_to_camel_case(ATTR_DEFINITIONS))]
            for attr in index_attrs:
                attr_name = attr.get(snake_to_camel_case(ATTR_NAME))
                if attr_name not in attr_keys:
                    schema[snake_to_camel_case(ATTR_DEFINITIONS)].append(attr)
                    attr_keys.append(attr_name)
            cls._get_connection().create_table(
                **schema
            )
        if wait:
            while True:
                status = cls._get_connection().describe_table()
                if status:
                    data = status.get(TABLE_STATUS)
                    if data == ACTIVE:
                        break
                    else:
                        time.sleep(2)
                else:
                    raise TableError("No TableStatus returned for table")

        cls.update_ttl(ignore_update_ttl_errors)

    @classmethod
    def update_ttl(cls, ignore_update_ttl_errors: bool) -> None:
        """
        Attempt to update the TTL on the table.
        Certain implementations (eg: dynalite) do not support updating TTLs and will fail.
        """
        ttl_attribute = cls._ttl_attribute()
        if ttl_attribute:
            # Some dynamoDB implementations (eg: dynalite) do not support updating TTLs so
            # this will fail.  It's fine for this to fail in those cases.
            try:
                cls._get_connection().update_time_to_live(ttl_attribute.attr_name)
            except Exception:
                if ignore_update_ttl_errors:
                    log.info("Unable to update the TTL for {}".format(cls.Meta.table_name))
                else:
                    raise

    @classmethod
    def dumps(cls) -> Any:
        """
        Returns a JSON representation of this model's table
        """
        return json.dumps([item._get_json() for item in cls.scan()])

    @classmethod
    def dump(cls, filename: str) -> None:
        """
        Writes the contents of this model's table as JSON to the given filename
        """
        with open(filename, 'w') as out:
            out.write(cls.dumps())

    @classmethod
    def loads(cls, data: str) -> None:
        content = json.loads(data)
        with cls.batch_write() as batch:
            for item_data in content:
                item = cls._from_data(item_data)
                batch.save(item)

    @classmethod
    def load(cls, filename: str) -> None:
        with open(filename, 'r') as inf:
            cls.loads(inf.read())

    # Private API below
    @classmethod
    def _from_data(cls, data):
        """
        Reconstructs a model object from JSON.
        """
        hash_key, attrs = data
        range_key = attrs.pop('range_key', None)
        attributes = attrs.pop(snake_to_camel_case(ATTRIBUTES))
        hash_key_attribute = cls._hash_key_attribute()
        hash_keyname = hash_key_attribute.attr_name
        hash_keytype = ATTR_TYPE_MAP[hash_key_attribute.attr_type]
        attributes[hash_keyname] = {
            hash_keytype: hash_key
        }
        if range_key is not None:
            range_key_attribute = cls._range_key_attribute()
            range_keyname = range_key_attribute.attr_name
            range_keytype = ATTR_TYPE_MAP[range_key_attribute.attr_type]
            attributes[range_keyname] = {
                range_keytype: range_key
            }
        item = cls(_user_instantiated=False)
        item._deserialize(attributes)
        return item

    @classmethod
    def _get_schema(cls):
        """
        Returns the schema for this table
        """
        schema: Dict[str, List] = {
            snake_to_camel_case(ATTR_DEFINITIONS): [],
            snake_to_camel_case(KEY_SCHEMA): []
        }
        for attr_name, attr_cls in cls.get_attributes().items():
            if attr_cls.is_hash_key or attr_cls.is_range_key:
                schema[snake_to_camel_case(ATTR_DEFINITIONS)].append({
                    snake_to_camel_case(ATTR_NAME): attr_cls.attr_name,
                    snake_to_camel_case(ATTR_TYPE): ATTR_TYPE_MAP[attr_cls.attr_type]
                })
            if attr_cls.is_hash_key:
                schema[snake_to_camel_case(KEY_SCHEMA)].append({
                    snake_to_camel_case(KEY_TYPE): HASH,
                    snake_to_camel_case(ATTR_NAME): attr_cls.attr_name
                })
            elif attr_cls.is_range_key:
                schema[snake_to_camel_case(KEY_SCHEMA)].append({
                    snake_to_camel_case(KEY_TYPE): RANGE,
                    snake_to_camel_case(ATTR_NAME): attr_cls.attr_name
                })
        return schema

    @classmethod
    def _get_indexes(cls):
        """
        Returns a list of the secondary indexes
        """
        if cls._indexes is None:
            cls._indexes = {
                snake_to_camel_case(GLOBAL_SECONDARY_INDEXES): [],
                snake_to_camel_case(LOCAL_SECONDARY_INDEXES): [],
                snake_to_camel_case(ATTR_DEFINITIONS): []
            }
            cls._index_classes = {}
            for name, index in getmembers(cls, lambda o: isinstance(o, Index)):
                cls._index_classes[index.Meta.index_name] = index
                schema = index._get_schema()
                idx = {
                    snake_to_camel_case(INDEX_NAME): index.Meta.index_name,
                    snake_to_camel_case(KEY_SCHEMA): schema.get(snake_to_camel_case(KEY_SCHEMA)),
                    snake_to_camel_case(PROJECTION): {
                        PROJECTION_TYPE: index.Meta.projection.projection_type,
                    },

                }
                if isinstance(index, GlobalSecondaryIndex):
                    if getattr(cls.Meta, 'billing_mode', None) != PAY_PER_REQUEST_BILLING_MODE:
                        idx[snake_to_camel_case(PROVISIONED_THROUGHPUT)] = {
                            READ_CAPACITY_UNITS: index.Meta.read_capacity_units,
                            WRITE_CAPACITY_UNITS: index.Meta.write_capacity_units
                        }
                cls._indexes[snake_to_camel_case(ATTR_DEFINITIONS)].extend(schema.get(snake_to_camel_case(ATTR_DEFINITIONS)))
                if index.Meta.projection.non_key_attributes:
                    idx[snake_to_camel_case(PROJECTION)][NON_KEY_ATTRIBUTES] = index.Meta.projection.non_key_attributes
                if isinstance(index, GlobalSecondaryIndex):
                    cls._indexes[snake_to_camel_case(GLOBAL_SECONDARY_INDEXES)].append(idx)
                else:
                    cls._indexes[snake_to_camel_case(LOCAL_SECONDARY_INDEXES)].append(idx)
        return cls._indexes

    def _get_json(self):
        """
        Returns a Python object suitable for serialization
        """
        kwargs = {}
        serialized = self._serialize(null_check=False)
        hash_key = serialized.get(HASH)
        range_key = serialized.get(RANGE, None)
        if range_key is not None:
            kwargs[snake_to_camel_case(RANGE_KEY)] = range_key
        kwargs[snake_to_camel_case(ATTRIBUTES)] = serialized[snake_to_camel_case(ATTRIBUTES)]
        return hash_key, kwargs

    def _get_save_args(self, attributes=True, null_check=True):
        """
        Gets the proper *args, **kwargs for saving and retrieving this object

        This is used for serializing items to be saved, or for serializing just the keys.

        :param attributes: If True, then attributes are included.
        :param null_check: If True, then attributes are checked for null.
        """
        kwargs = {}
        serialized = self._serialize(null_check=null_check)
        hash_key = serialized.get(HASH)
        range_key = serialized.get(RANGE, None)
        args = (hash_key, )
        if range_key is not None:
            kwargs[snake_to_camel_case(RANGE_KEY)] = range_key
        if attributes:
            kwargs[snake_to_camel_case(ATTRIBUTES)] = serialized[snake_to_camel_case(ATTRIBUTES)]
        return args, kwargs

    def _handle_version_attribute(self, serialized_attributes, actions=None):
        """
        Handles modifying the request to set or increment the version attribute.

        :param serialized_attributes: A dictionary mapping attribute names to serialized values.
        :param actions: A non-empty list when performing an update, otherwise None.
        """
        if self._version_attribute_name is None:
            return

        version_attribute = self.get_attributes()[self._version_attribute_name]
        version_attribute_value = getattr(self, self._version_attribute_name)

        if version_attribute_value:
            version_condition = version_attribute == version_attribute_value
            if actions:
                actions.append(version_attribute.add(1))
            elif snake_to_camel_case(ATTRIBUTES) in serialized_attributes:
                serialized_attributes[snake_to_camel_case(ATTRIBUTES)][version_attribute.attr_name] = self._serialize_value(
                    version_attribute, version_attribute_value + 1, null_check=True
                )
        else:
            version_condition = version_attribute.does_not_exist()
            if actions:
                actions.append(version_attribute.set(1))
            elif snake_to_camel_case(ATTRIBUTES) in serialized_attributes:
                serialized_attributes[snake_to_camel_case(ATTRIBUTES)][version_attribute.attr_name] = self._serialize_value(
                    version_attribute, 1, null_check=True
                )

        return version_condition

    def update_local_version_attribute(self):
        if self._version_attribute_name is not None:
            value = getattr(self, self._version_attribute_name, None) or 0
            setattr(self, self._version_attribute_name, value + 1)

    @classmethod
    def _hash_key_attribute(cls):
        """
        Returns the attribute class for the hash key
        """
        return cls.get_attributes()[cls._hash_keyname] if cls._hash_keyname else None

    @classmethod
    def _range_key_attribute(cls):
        """
        Returns the attribute class for the range key
        """
        return cls.get_attributes()[cls._range_keyname] if cls._range_keyname else None

    @classmethod
    def _ttl_attribute(cls):
        """
        Returns the ttl attribute for this table
        """
        attributes = cls.get_attributes()
        for attr_obj in attributes.values():
            if isinstance(attr_obj, TTLAttribute):
                return attr_obj
        return None

    def _get_keys(self):
        """
        Returns the proper arguments for deleting
        """
        serialized = self._serialize(null_check=False)
        hash_key = serialized.get(HASH)
        range_key = serialized.get(RANGE, None)
        attrs = {
            self._hash_key_attribute().attr_name: hash_key,
        }
        if self._range_keyname is not None:
            range_keyname = self._range_key_attribute().attr_name
            attrs[range_keyname] = range_key
        return attrs

    @classmethod
    def _batch_get_page(cls, keys_to_get, consistent_read, attributes_to_get):
        """
        Returns a single page from BatchGetItem
        Also returns any unprocessed items

        :param keys_to_get: A list of keys
        :param consistent_read: Whether or not this needs to be consistent
        :param attributes_to_get: A list of attributes to return
        """
        log.debug("Fetching a BatchGetItem page")
        data = cls._get_connection().batch_get_item(
            keys_to_get, consistent_read=consistent_read, attributes_to_get=attributes_to_get
        )
        item_data = data.get(RESPONSES).get(cls.Meta.table_name)  # type: ignore
        unprocessed_items = data.get(UNPROCESSED_KEYS).get(cls.Meta.table_name, {}).get(KEYS, None)  # type: ignore
        return item_data, unprocessed_items

    _connection_class = TableConnection

    @classmethod
    def _get_connection(cls) -> TableConnection:
        """
        Returns a (cached) connection
        """
        if not hasattr(cls, "Meta"):
            raise AttributeError(
                'As of v1.0 PynamoDB Models require a `Meta` class.\n'
                'Model: {}.{}\n'
                'See https://pynamodb.readthedocs.io/en/latest/release_notes.html\n'.format(
                    cls.__module__, cls.__name__,
                ),
            )
        elif not hasattr(cls.Meta, "table_name") or cls.Meta.table_name is None:
            raise AttributeError(
                'As of v1.0 PyanmoDB Models must have a table_name\n'
                'Model: {}.{}\n'
                'See https://pynamodb.readthedocs.io/en/latest/release_notes.html'.format(
                    cls.__module__, cls.__name__,
                ),
            )
        if cls._connection is None:
            cls._connection = cls._connection_class(cls.Meta.table_name,
                                              region=cls.Meta.region,
                                              host=cls.Meta.host,
                                              connect_timeout_seconds=cls.Meta.connect_timeout_seconds,
                                              read_timeout_seconds=cls.Meta.read_timeout_seconds,
                                              max_retry_attempts=cls.Meta.max_retry_attempts,
                                              base_backoff_ms=cls.Meta.base_backoff_ms,
                                              max_pool_connections=cls.Meta.max_pool_connections,
                                              extra_headers=cls.Meta.extra_headers,
                                              aws_access_key_id=cls.Meta.aws_access_key_id,
                                              aws_secret_access_key=cls.Meta.aws_secret_access_key,
                                              aws_session_token=cls.Meta.aws_session_token)
        return cls._connection

    def _deserialize(self, attrs):
        """
        Sets attributes sent back from DynamoDB on this object

        :param attrs: A dictionary of attributes to update this item with.
        """
        for name, attr in self.get_attributes().items():
            value = attrs.get(attr.attr_name, None)
            if value is not None:
                value = value.get(ATTR_TYPE_MAP[attr.attr_type], None)
                if value is not None:
                    value = attr.deserialize(value)
            setattr(self, name, value)

    def _serialize(self, attr_map=False, null_check=True) -> Dict[str, Any]:
        """
        Serializes all model attributes for use with DynamoDB

        :param attr_map: If True, then attributes are returned
        :param null_check: If True, then attributes are checked for null
        """
        attributes = snake_to_camel_case(ATTRIBUTES)
        attrs: Dict[str, Dict] = {attributes: {}}
        for name, attr in self.get_attributes().items():
            value = getattr(self, name)
            if isinstance(value, MapAttribute):
                if not value.validate():
                    raise ValueError("Attribute '{}' is not correctly typed".format(attr.attr_name))

            serialized = self._serialize_value(attr, value, null_check)
            if NULL in serialized:
                continue

            if attr_map:
                attrs[attributes][attr.attr_name] = serialized
            else:
                if attr.is_hash_key:
                    attrs[HASH] = serialized[ATTR_TYPE_MAP[attr.attr_type]]
                elif attr.is_range_key:
                    attrs[RANGE] = serialized[ATTR_TYPE_MAP[attr.attr_type]]
                else:
                    attrs[attributes][attr.attr_name] = serialized

        return attrs

    @classmethod
    def _serialize_value(cls, attr, value, null_check=True):
        """
        Serializes a value for use with DynamoDB

        :param attr: an instance of `Attribute` for serialization
        :param value: a value to be serialized
        :param null_check: If True, then attributes are checked for null
        """
        if value is None:
            serialized = None
        else:
            serialized = attr.serialize(value)

        if serialized is None:
            if not attr.null and null_check:
                raise ValueError("Attribute '{}' cannot be None".format(attr.attr_name))
            return {NULL: True}

        return {ATTR_TYPE_MAP[attr.attr_type]: serialized}

    @classmethod
    def _serialize_keys(cls, hash_key, range_key=None) -> Tuple[_KeyType, _KeyType]:
        """
        Serializes the hash and range keys

        :param hash_key: The hash key value
        :param range_key: The range key value
        """
        hash_key = cls._hash_key_attribute().serialize(hash_key)
        if range_key is not None:
            range_key = cls._range_key_attribute().serialize(range_key)
        return hash_key, range_key


class _ModelFuture(Generic[_T]):
    """
    A placeholder object for a model that does not exist yet

    For example: when performing a TransactGet request, this is a stand-in for a model that will be returned
    when the operation is complete
    """
    def __init__(self, model_cls: Type[_T]) -> None:
        self._model_cls = model_cls
        self._model: Optional[_T] = None
        self._resolved = False

    def update_with_raw_data(self, data: Dict[str, Any]) -> None:
        if data is not None and data != {}:
            self._model = self._model_cls.from_raw_data(data=data)
        self._resolved = True

    def get(self) -> _T:
        if not self._resolved:
            raise InvalidStateError()
        if self._model:
            return self._model
        raise self._model_cls.DoesNotExist()
