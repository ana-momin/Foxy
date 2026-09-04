"""Validate action parameters against the schema the manifest advertises.

The manifest declared types, enums and `additionalProperties: false`, and
nothing checked any of it. Parameters were read with `params.get(...)`, so a
misspelled field was silently ignored, a string where a number belonged raised
a ValueError that surfaced as an internal error, and a source name outside the
enum was accepted and then quietly dropped.

Advertising a schema and not enforcing it is worse than not advertising one:
the caller is told the contract and then finds it is not kept. This checks the
declared schema itself, so the two can never drift apart.
"""

from __future__ import annotations

from typing import Any

# The subset of JSON Schema the manifest actually uses. Deliberately small:
# a general validator is a dependency and a surprise, while these are the rules
# we publish and can therefore be held to.
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class Invalid(Exception):
    """A parameter did not match the advertised schema."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def _check_number(name: str, value: Any, spec: dict[str, Any]) -> Any:
    want_int = spec["type"] == "integer"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(name, f"'{name}' must be a {spec['type']}.")
    if want_int and not float(value).is_integer():
        raise Invalid(name, f"'{name}' must be a whole number.")
    value = int(value) if want_int else float(value)
    if "minimum" in spec and value < spec["minimum"]:
        raise Invalid(name, f"'{name}' must be at least {spec['minimum']}.")
    if "maximum" in spec and value > spec["maximum"]:
        raise Invalid(name, f"'{name}' must be at most {spec['maximum']}.")
    return value


def _check_one(name: str, value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")

    if kind in {"integer", "number"}:
        return _check_number(name, value, spec)

    expected = _TYPES.get(kind)
    if expected and not isinstance(value, expected):
        raise Invalid(name, f"'{name}' must be a {kind}.")
    # bool is an int in Python, and a boolean is not a string.
    if kind == "string" and isinstance(value, bool):
        raise Invalid(name, f"'{name}' must be a string.")

    if kind == "array":
        item = spec.get("items") or {}
        return [_check_one(f"{name}[{i}]", v, item) for i, v in enumerate(value)]

    if "enum" in spec and value not in spec["enum"]:
        allowed = ", ".join(str(x) for x in spec["enum"])
        raise Invalid(name, f"'{value}' is not one of: {allowed}.")

    return value


def validate(params: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Check `params` against `schema`, returning the coerced values.

    Raises `Invalid` with the offending field, so the caller can answer with a
    Pond error that names it rather than a generic failure.
    """
    if not isinstance(params, dict):
        raise Invalid("parameters", "Parameters must be an object.")

    properties: dict[str, Any] = schema.get("properties") or {}

    if schema.get("additionalProperties") is False:
        for key in params:
            if key not in properties:
                known = ", ".join(sorted(properties)) or "none"
                raise Invalid(
                    f"parameters.{key}",
                    f"Unknown parameter '{key}'. Accepted: {known}.",
                )

    for name in schema.get("required") or []:
        if params.get(name) in (None, ""):
            raise Invalid(f"parameters.{name}", f"'{name}' is required.")

    clean: dict[str, Any] = {}
    for name, value in params.items():
        spec = properties.get(name)
        if spec is None or value is None:
            continue
        try:
            clean[name] = _check_one(name, value, spec)
        except Invalid as exc:
            raise Invalid(f"parameters.{exc.field}", exc.message) from None
    return clean
