"""Validation and fixed-code compilation for subworkflow field mappings."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_FORBIDDEN_NAMES = {"__proto__", "constructor", "prototype"}
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_DEFINITION_BYTES = 32_768


def _invalid() -> ValueError:
    # Keep validation errors independent of user supplied data.
    return ValueError("invalid subworkflow mapping")


def _valid_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _IDENTIFIER.fullmatch(value) is not None
        and value not in _FORBIDDEN_NAMES
    )


def _validate_literal(value: Any, *, depth: int, budget: list[int]) -> None:
    budget[0] += 1
    if budget[0] > 256 or depth > 8:
        raise _invalid()

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > 4000 or "\x00" in value:
            raise _invalid()
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise _invalid()
        return
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _MAX_SAFE_INTEGER:
            raise _invalid()
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise _invalid()
        for item in value:
            _validate_literal(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        if len(value) > 32:
            raise _invalid()
        for key, item in value.items():
            if not _valid_name(key):
                raise _invalid()
            _validate_literal(item, depth=depth + 1, budget=budget)
        return
    raise _invalid()


def validate_mapping(value: Any) -> dict[str, Any]:
    """Return a detached, validated mapping definition."""
    try:
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "fields"}:
            raise _invalid()
        version = value["schemaVersion"]
        fields = value["fields"]
        if type(version) is not int or version != 1:
            raise _invalid()
        if not isinstance(fields, list) or not 1 <= len(fields) <= 32:
            raise _invalid()

        names: set[str] = set()
        # Reserve one output node for the result root and one for each field.
        literal_budget = [1 + sum(field.get("kind") == "path" for field in fields if isinstance(field, dict))]
        if literal_budget[0] > 256:
            raise _invalid()
        for field in fields:
            if not isinstance(field, dict):
                raise _invalid()
            name = field.get("name")
            kind = field.get("kind")
            if not _valid_name(name) or name in names:
                raise _invalid()
            names.add(name)
            if kind == "path":
                if set(field) != {"name", "kind", "path"}:
                    raise _invalid()
                path = field["path"]
                if not isinstance(path, list) or not 1 <= len(path) <= 8:
                    raise _invalid()
                if any(not _valid_name(part) for part in path):
                    raise _invalid()
            elif kind == "literal":
                if set(field) != {"name", "kind", "value"}:
                    raise _invalid()
                _validate_literal(field["value"], depth=1, budget=literal_budget)
            else:
                raise _invalid()

        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_DEFINITION_BYTES:
            raise _invalid()
        return copy.deepcopy(value)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise _invalid() from None


def compile_mapping(
    value: Any,
    source_expression: str,
    *,
    result_key: str,
) -> str:
    """Compile a validated mapping into a fixed JavaScript projection function."""
    mapping = validate_mapping(value)
    if (
        not isinstance(source_expression, str)
        or not isinstance(result_key, str)
        or result_key not in {"input", "output"}
    ):
        raise _invalid()

    statements = [
        "return (function () {",
        f"  const source = {source_expression};",
        '  const fail = function (code) { throw new Error("SUBWORKFLOW_MAPPING_" + code); };',
        "  const isObject = function (value) {",
        '    if (value === null || typeof value !== "object" || Array.isArray(value)) return false;',
        "    const prototype = Object.getPrototypeOf(value);",
        "    return prototype === Object.prototype || prototype === null;",
        "  };",
        '  if (!isObject(source)) fail("SOURCE");',
        "  const validate = function (value, depth, budget) {",
        '    if (depth > 8 || ++budget.count > 256) fail("LIMIT");',
        '    if (value === null || typeof value === "boolean") return;',
        '    if (typeof value === "string") { if (value.indexOf("\\u0000") !== -1 || Array.from(value).length > 4000) fail("VALUE"); return; }',
        '    if (typeof value === "number") { if (!Number.isFinite(value) || Math.abs(value) > 9007199254740991) fail("VALUE"); return; }',
        '    if (Array.isArray(value)) { if (value.length > 100) fail("LIMIT"); for (const item of value) validate(item, depth + 1, budget); return; }',
        '    if (!isObject(value)) fail("VALUE");',
        '    const keys = Object.keys(value); if (keys.length > 32) fail("LIMIT");',
        '    for (const key of keys) { if (key.length > 64 || !/^[A-Za-z_]/.test(key) || /[^A-Za-z0-9_]/.test(key) || key === "__proto__" || key === "constructor" || key === "prototype") fail("VALUE"); validate(value[key], depth + 1, budget); }',
        "  };",
        "  const projection = Object.create(null);",
    ]

    for index, field in enumerate(mapping["fields"]):
        if field["kind"] == "path":
            current = f"value{index}"
            statements.append(f"  let {current} = source;")
            for segment_index, segment in enumerate(field["path"]):
                statements.append(f'  if (!isObject({current}) || !Object.prototype.hasOwnProperty.call({current}, {json.dumps(segment)})) fail("PATH");')
                next_value = f"value{index}_{segment_index}"
                statements.append(f"  const {next_value} = {current}[{json.dumps(segment)}];")
                current = next_value
            expression = current
        else:
            expression = json.dumps(
                field["value"], ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            expression = expression.replace("<", r"\u003c").replace("{{", r"\u007b\u007b")
        statements.append(f"  projection[{json.dumps(field['name'])}] = {expression};")

    statements.extend(
        [
            "  const budget = { count: 0 };",
            "  validate(projection, 0, budget);",
            "  let serialized;",
            '  try { serialized = JSON.stringify(projection); } catch (_) { fail("SERIALIZE"); }',
            '  if (typeof serialized !== "string") fail("SERIALIZE");',
            "  let utf8Bytes = 0;",
            "  for (const character of serialized) {",
            "    const codePoint = character.codePointAt(0);",
            "    utf8Bytes += codePoint <= 0x7f ? 1 : codePoint <= 0x7ff ? 2 : codePoint <= 0xffff ? 3 : 4;",
            '    if (utf8Bytes > 65536) fail("SIZE");',
            "  }",
            f"  return {{{result_key}: projection}};",
            "})();",
        ]
    )
    return "\n".join(statements)
