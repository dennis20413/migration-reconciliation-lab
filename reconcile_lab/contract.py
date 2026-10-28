"""Validate contracts before processing data; SQL identifiers are always quoted."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


class InputError(ValueError):
    """An input cannot be interpreted under the declared contract."""


GATES = {
    "max_invalid_key_rows": 0,
    "max_duplicate_rows": 0,
    "max_invalid_value_rows": 0,
    "max_unmatched_rows": 0,
    "max_changed_rows": 0,
}


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def load_contract(path: Path) -> dict:
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(c, dict):
            raise InputError("Contract must be a JSON object")
        keys = c["keys"]
        if not isinstance(keys, dict):
            raise InputError("keys must be an object")
        columns = keys["columns"]
        if not isinstance(columns, list) or not columns:
            raise InputError("keys.columns must be a non-empty list")
        if keys.get("case", "none") not in ("none", "upper"):
            raise InputError("keys.case must be none or upper")
        if not isinstance(keys.get("trim", True), bool):
            raise InputError("keys.trim must be boolean")
        fields = c["fields"]
        if not isinstance(fields, list) or not fields:
            raise InputError("fields must be a non-empty list")
        if any(not isinstance(f, dict) for f in fields):
            raise InputError("Every field must be an object")
        names = columns + [f["name"] for f in fields]
        if any(not isinstance(n, str) or not n or n.startswith("_") for n in names):
            raise InputError("Column names must be non-empty strings and cannot begin with _")
        if len(names) != len(set(names)) or len({n.lower() for n in names}) != len(names):
            raise InputError("Contract columns must be unique, including case-insensitively")
        nulls = c.get("null_tokens", ["", "NULL"])
        if not isinstance(nulls, list) or any(not isinstance(n, str) for n in nulls):
            raise InputError("null_tokens must be a list of exact, case-sensitive strings")
        c["null_tokens"] = nulls
        for f in fields:
            if f.get("type") not in ("string", "decimal"):
                raise InputError("Each field type must be string or decimal")
            if not isinstance(f.get("trim", True), bool):
                raise InputError("Field trim must be boolean")
            if f["type"] == "decimal":
                scale = f.get("scale", 2)
                if type(scale) is not int or not 0 <= scale <= 12:
                    raise InputError("Decimal scale must be an integer between 0 and 12")
                tolerance = Decimal(str(f.get("tolerance", "0")))
                quantum = Decimal(1).scaleb(-scale)
                if not tolerance.is_finite() or tolerance < 0 or tolerance % quantum != 0:
                    raise InputError("Tolerance must be finite, non-negative, and exact at the declared scale")
                if tolerance >= Decimal(10) ** (28 - scale):
                    raise InputError("Tolerance exceeds DECIMAL(28, scale)")
                f["scale"] = scale
                f["tolerance"] = format(tolerance, "f")
        gates = c.get("gates", {})
        if not isinstance(gates, dict) or set(gates) - set(GATES):
            raise InputError("Unknown quality gate")
        if any(type(v) is not int or v < 0 for v in gates.values()):
            raise InputError("Quality gates must be non-negative integers")
        c["gates"] = GATES | gates
        return c
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, InvalidOperation) as exc:
        raise InputError(f"Invalid contract: {exc}") from exc


def validate_memory_limit(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*\s*(MB|GB)", value, re.IGNORECASE):
        raise InputError("Memory limit must look like 256MB or 1GB")
    return value.upper().replace(" ", "")
