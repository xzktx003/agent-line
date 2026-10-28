"""Canonical validation helpers for frozen static-batch agent inputs."""
import hashlib
import json
import math
import re


MAX_INPUT_BYTES = 1200
RECIPE_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
ITEM_ID = re.compile(r'^item_[0-9a-f]{24}$')
INPUT_HASH = re.compile(r'^[0-9a-f]{64}$')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _finite_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('batch_item_invalid')
        return
    if type(value) is list:
        for item in value:
            _finite_json(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError('batch_item_invalid')
        for item in value.values():
            _finite_json(item)
        return
    raise ValueError('batch_item_invalid')


def _identity_digest(recipe_id, index, input_sha256):
    value = f'{recipe_id}\0{index}\0{input_sha256}'.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def validate_batch_item(value):
    """Validate exact shape, digest and size, returning a detached value."""
    try:
        if type(value) is not dict or set(value) != {
                'recipeId', 'itemId', 'index', 'inputSha256', 'input'}:
            raise ValueError('batch_item_invalid')
        recipe_id = value['recipeId']
        item_id = value['itemId']
        index = value['index']
        input_sha256 = value['inputSha256']
        if (type(recipe_id) is not str or not RECIPE_ID.fullmatch(recipe_id)
                or type(item_id) is not str or not ITEM_ID.fullmatch(item_id)
                or type(index) is not int or not 1 <= index <= 20
                or type(input_sha256) is not str or not INPUT_HASH.fullmatch(input_sha256)):
            raise ValueError('batch_item_invalid')
        _finite_json(value['input'])
        encoded = _canonical(value['input']).encode('utf-8')
        if len(encoded) > MAX_INPUT_BYTES or hashlib.sha256(encoded).hexdigest() != input_sha256:
            raise ValueError('batch_item_invalid')
        digest = _identity_digest(recipe_id, index, input_sha256)
        if item_id != 'item_' + digest[:24]:
            raise ValueError('batch_item_invalid')
        return json.loads(_canonical(value))
    except (TypeError, ValueError, KeyError, UnicodeError, OverflowError,
            RecursionError):
        raise ValueError('batch_item_invalid') from None


def batch_node_id(value):
    batch_item = validate_batch_item(value)
    digest = _identity_digest(batch_item['recipeId'], batch_item['index'],
                              batch_item['inputSha256'])
    return 'item_' + digest[:32]


def batch_summary(value):
    batch_item = validate_batch_item(value)
    return {key: batch_item[key] for key in ('recipeId', 'itemId', 'index', 'inputSha256')}


def task_batch_item(payload):
    """Validate optional trusted metadata against task input and native node."""
    if 'batchItem' not in payload:
        return None
    from .sim_authoring import native_id
    try:
        item = validate_batch_item(payload['batchItem'])
        if (_canonical(payload['input']['batchItem']) != _canonical(item)
                or payload['workflow']['nodeId'] != native_id(
                    payload['workflow']['id'], batch_node_id(item))):
            raise ValueError('batch_item_invalid')
        return item
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise ValueError('batch_item_invalid') from None
