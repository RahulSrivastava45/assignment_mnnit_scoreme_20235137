import pytest

from decision_system.rules import evaluate, RuleError
from decision_system.schema import validate, ValidationError

SCHEMA = {
    "fields": {
        "name": {"type": "string", "required": True, "min_length": 2},
        "age": {"type": "integer", "required": True, "min": 0, "max": 120},
        "kind": {"type": "string", "enum": ["a", "b"]},
        "when": {"type": "date"},
    }
}


def test_valid_payload_passes():
    data = {"name": "Asha", "age": 30, "kind": "a", "when": "2025-01-31"}
    assert validate(data, SCHEMA) == data


def test_missing_required_field():
    with pytest.raises(ValidationError) as exc:
        validate({"name": "Asha"}, SCHEMA)
    assert "age: field is required" in exc.value.errors


def test_wrong_types_and_ranges_reported_together():
    with pytest.raises(ValidationError) as exc:
        validate({"name": "A", "age": "x", "kind": "z", "when": "31-01-2025"}, SCHEMA)
    assert len(exc.value.errors) == 4


def test_bool_is_not_an_integer():
    with pytest.raises(ValidationError):
        validate({"name": "Asha", "age": True}, SCHEMA)


def test_unknown_fields_rejected_by_default():
    with pytest.raises(ValidationError):
        validate({"name": "Asha", "age": 3, "extra": 1}, SCHEMA)


CTX = {"data": {"age": 25, "income": 1000, "loan": 4000, "tags": ["x"]}}


def run(cond):
    trace = []
    return evaluate(cond, CTX, trace), trace


def test_simple_comparison_and_trace():
    ok, trace = run({"field": "data.age", "op": ">=", "value": 18})
    assert ok
    assert trace == [{"field": "data.age", "op": ">=", "expected": 18, "actual": 25, "result": True}]


def test_all_any_not():
    assert run({"all": [{"field": "data.age", "op": ">", "value": 18},
                        {"field": "data.income", "op": "<", "value": 10}]})[0] is False
    assert run({"any": [{"field": "data.age", "op": ">", "value": 99},
                        {"field": "data.income", "op": "==", "value": 1000}]})[0] is True
    assert run({"not": {"field": "data.age", "op": "<", "value": 18}})[0] is True


def test_ref_with_multiplier():
    cond = {"field": "data.loan", "op": ">", "value": {"ref": "data.income", "multiply": 3}}
    ok, trace = run(cond)
    assert ok
    assert trace[0]["expected"] == 3000


def test_missing_field_never_matches():
    ok, trace = run({"field": "credit.score", "op": "<", "value": 600})
    assert not ok and trace[0]["missing"]


def test_type_mismatch_is_false_not_crash():
    ok, trace = run({"field": "data.tags", "op": ">", "value": 3})
    assert not ok and trace[0]["error"] == "type mismatch"


def test_unknown_operator():
    with pytest.raises(RuleError):
        run({"field": "data.age", "op": "~~", "value": 1})
