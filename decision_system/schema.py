"""Request validation driven by the schema block of a workflow config."""
from datetime import date


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _is_date(v):
    if not isinstance(v, str):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "date": _is_date,
}


def validate(payload, schema):
    """Check payload against schema, returning the payload or raising ValidationError.

    Schema shape:
        {"fields": {"age": {"type": "integer", "required": true, "min": 0}},
         "allow_unknown": false}
    """
    if not isinstance(payload, dict):
        raise ValidationError(["request data must be a JSON object"])

    errors = []
    fields = schema.get("fields", {})

    for name, rule in fields.items():
        if name not in payload or payload[name] is None:
            if rule.get("required", False):
                errors.append(f"{name}: field is required")
            continue

        value = payload[name]
        ftype = rule.get("type", "string")
        check = _TYPES.get(ftype)
        if check is None:
            errors.append(f"{name}: unknown type '{ftype}' in schema")
            continue
        if not check(value):
            errors.append(f"{name}: expected {ftype}, got {type(value).__name__}")
            continue

        if "min" in rule and value < rule["min"]:
            errors.append(f"{name}: must be >= {rule['min']}")
        if "max" in rule and value > rule["max"]:
            errors.append(f"{name}: must be <= {rule['max']}")
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{name}: must be one of {rule['enum']}")
        if "min_length" in rule and len(value) < rule["min_length"]:
            errors.append(f"{name}: must have at least {rule['min_length']} characters")

    if not schema.get("allow_unknown", False):
        for name in payload:
            if name not in fields:
                errors.append(f"{name}: unknown field")

    if errors:
        raise ValidationError(errors)
    return payload
