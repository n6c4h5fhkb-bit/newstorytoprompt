"""Small JSON-schema subset used by this repository; no model calls."""
from __future__ import annotations
import math
import re

from storyforge import ROOT
from storyforge.config import SflError, load_yaml


def schema_for(name: str) -> dict:
    return load_yaml(ROOT / "platform" / "schemas.yaml")["schemas"][name]


def validate(value, schema: dict, path: str = "$") -> list[str]:
    errors = []
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    allowed = kind if isinstance(kind, list) else [kind]
    matches = any(isinstance(value, types[t]) and not (t in ("integer", "number") and isinstance(value, bool)) for t in allowed)
    if not matches:
        return [f"{path}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors.extend(validate(item, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field {key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: too many items")
        if schema.get("uniqueItems") and len({repr(x) for x in value}) != len(value):
            errors.append(f"{path}: duplicate items")
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: empty text")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: pattern mismatch")
    elif kind in ("integer", "number"):
        if not math.isfinite(value):
            errors.append(f"{path}: non-finite number")
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must exceed {schema['exclusiveMinimum']}")
    return errors


def require(value, schema: dict):
    errors = validate(value, schema)
    if errors:
        raise SflError("Invalid structured output: " + "; ".join(errors[:20]))
    return value
