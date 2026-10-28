"""Validation helpers for portable fields-result-v2 declarations."""

import json
import math
import re


_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_SCALARS = {"string", "boolean", "number", "integer", "string_array"}
_FORBIDDEN_NAMES = {"constructor", "prototype", "__proto__"}
_MAX_SAFE = 9_007_199_254_740_991
_MAX_DEFINITION_BYTES = 16 * 1024


def _fail():
    raise ValueError("Invalid structured field definition or result")


def _canonical_size(value):
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail()
    if len(encoded) > _MAX_DEFINITION_BYTES:
        _fail()


def validate_definition(value):
    """Validate and return a detached, exact fields-result-v2 definition."""
    _canonical_size(value)
    if type(value) is not dict or set(value) != {"fields"}:
        _fail()
    node_count = 0

    def fields_list(raw, depth):
        nonlocal node_count
        if depth > 4 or type(raw) is not list or not 1 <= len(raw) <= 32:
            _fail()
        clean, names, has_required = [], set(), False
        for field in raw:
            if (type(field) is not dict
                    or not {"name", "type", "required"} <= set(field) <=
                    {"name", "type", "required", "fields", "items"}):
                _fail()
            name, kind, required = field["name"], field["type"], field["required"]
            if (type(name) is not str or _FIELD_NAME.fullmatch(name) is None
                    or name in _FORBIDDEN_NAMES or name in names
                    or type(required) is not bool or type(kind) is not str):
                _fail()
            names.add(name)
            has_required = has_required or required
            node_count += 1
            if node_count > 128:
                _fail()
            if kind in _SCALARS:
                if set(field) != {"name", "type", "required"}:
                    _fail()
                clean.append({"name": name, "type": kind, "required": required})
            elif kind == "object":
                if set(field) != {"name", "type", "required", "fields"}:
                    _fail()
                nested = fields_list(field["fields"], depth + 1)
                clean.append({"name": name, "type": kind, "required": required, "fields": nested})
            elif kind == "array":
                if set(field) != {"name", "type", "required", "items"}:
                    _fail()
                item = item_spec(field["items"], depth + 1)
                clean.append({"name": name, "type": kind, "required": required, "items": item})
            else:
                _fail()
        if not has_required:
            _fail()
        return clean

    def item_spec(raw, depth):
        nonlocal node_count
        if depth > 4 or type(raw) is not dict or "type" not in raw:
            _fail()
        kind = raw["type"]
        if type(kind) is not str:
            _fail()
        node_count += 1
        if node_count > 128:
            _fail()
        if kind in _SCALARS:
            if set(raw) != {"type"}:
                _fail()
            return {"type": kind}
        if kind == "object":
            if set(raw) != {"type", "fields"}:
                _fail()
            return {"type": kind, "fields": fields_list(raw["fields"], depth + 1)}
        if kind == "array":
            if set(raw) != {"type", "items"}:
                _fail()
            return {"type": kind, "items": item_spec(raw["items"], depth + 1)}
        _fail()

    result = {"fields": fields_list(value["fields"], 1)}
    _canonical_size(result)
    return result


def _validate_value(value, kind_spec):
    kind = kind_spec["type"]
    if kind == "string":
        valid = type(value) is str and len(value) <= 4000 and "\0" not in value
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind in ("number", "integer"):
        valid = type(value) in ((int,) if kind == "integer" else (int, float))
        if valid:
            valid = abs(value) <= _MAX_SAFE and (type(value) is int or math.isfinite(value))
    elif kind == "string_array":
        valid = (type(value) is list and len(value) <= 100
                 and all(type(item) is str and len(item) <= 2000 and "\0" not in item for item in value))
    elif kind == "object":
        valid = _validate_object(value, kind_spec["fields"])
    elif kind == "array":
        valid = (type(value) is list and len(value) <= 100
                 and all(_validate_value(item, kind_spec["items"]) for item in value))
    else:
        valid = False
    if not valid:
        _fail()
    return True


def _validate_object(value, fields):
    if type(value) is not dict:
        _fail()
    declared = {field["name"] for field in fields}
    required = {field["name"] for field in fields if field["required"]}
    if not required <= set(value) <= declared:
        _fail()
    for field in fields:
        if field["name"] in value:
            _validate_value(value[field["name"]], field)
    return True


def validate_result(result, definition):
    """Validate a result object recursively against its declared shape."""
    clean = validate_definition(definition)
    _validate_object(result, clean["fields"])
    return result


def scalar_fields(definition):
    """List required scalar result paths beneath only required objects."""
    clean = validate_definition(definition)
    output = []

    def visit(fields, prefix):
        for field in fields:
            if not field["required"]:
                continue
            path = prefix + [field["name"]]
            if field["type"] in {"string", "boolean", "number", "integer"}:
                output.append({"path": path, "type": field["type"]})
            elif field["type"] == "object":
                visit(field["fields"], path)

    visit(clean["fields"], [])
    return output
