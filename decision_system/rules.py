"""Rule condition evaluator.

Conditions are plain JSON so they can live in config files:

    {"field": "data.age", "op": ">=", "value": 18}
    {"all": [<cond>, <cond>]}   {"any": [...]}   {"not": <cond>}

A value can also point at another field: {"ref": "data.income", "multiply": 5}.
Every leaf check is recorded in a trace so decisions can be explained later.
"""

MISSING = object()


class RuleError(Exception):
    pass


def resolve(path, ctx):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def _value(spec, ctx):
    if isinstance(spec, dict) and "ref" in spec:
        v = resolve(spec["ref"], ctx)
        if v is not MISSING and "multiply" in spec:
            if isinstance(v, (int, float)):
                v = v * spec["multiply"]
            else:
                return MISSING
        return v
    return spec


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in a,
}


def evaluate(cond, ctx, trace):
    """Evaluate cond against ctx; append one entry per leaf check to trace."""
    if "all" in cond:
        results = [evaluate(c, ctx, trace) for c in cond["all"]]
        return all(results)
    if "any" in cond:
        results = [evaluate(c, ctx, trace) for c in cond["any"]]
        return any(results)
    if "not" in cond:
        return not evaluate(cond["not"], ctx, trace)

    field, op = cond.get("field"), cond.get("op")
    if field is None or op is None:
        raise RuleError(f"invalid condition: {cond}")

    actual = resolve(field, ctx)
    entry = {"field": field, "op": op}

    if op == "exists":
        result = actual is not MISSING and actual is not None
        entry.update(expected=None, actual=None if actual is MISSING else actual)
    else:
        if op not in _OPS:
            raise RuleError(f"unknown operator '{op}'")
        expected = _value(cond.get("value"), ctx)
        entry["expected"] = None if expected is MISSING else expected
        if actual is MISSING or expected is MISSING:
            # a check against data we don't have can never pass
            entry["actual"] = None
            entry["missing"] = True
            result = False
        else:
            entry["actual"] = actual
            try:
                result = bool(_OPS[op](actual, expected))
            except TypeError:
                entry["error"] = "type mismatch"
                result = False

    entry["result"] = result
    trace.append(entry)
    return result
