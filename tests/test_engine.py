import json

import pytest

from decision_system.engine import IdempotencyConflict, InvalidOperation, NotFound
from decision_system.schema import ValidationError
from decision_system.store import StoreError
from decision_system.workflow import ConfigError, WorkflowRegistry

from conftest import loan, claim


# ---- happy path ---------------------------------------------------------

def test_loan_approved_happy_path(engine):
    result = engine.submit("loan_application", loan())
    rec = result.record
    assert not result.replayed
    assert rec["status"] == "APPROVED"
    assert rec["context"]["credit"]["score"] == 780

    states = [h["to_status"] for h in engine.history(rec["id"])]
    assert states == ["RECEIVED", "IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS", "APPROVED"]


def test_rejection_rule_short_circuits_before_dependency(engine):
    rec = engine.submit("loan_application", loan(age=17)).record
    assert rec["status"] == "REJECTED"
    assert "credit" not in rec["context"]          # credit bureau never called
    exp = engine.explain(rec["id"])
    assert exp["reasons"] == ["MIN_AGE: Applicant must be at least 18"]


def test_multi_step_evaluation_ends_in_manual_review(engine):
    rec = engine.submit("loan_application", loan(applicant_id="APP-BORDER")).record
    assert rec["status"] == "MANUAL_REVIEW"
    trace = engine.explain(rec["id"])["rule_trace"]
    stages = [t["stage"] for t in trace]
    assert "mandatory_checks" in stages and "affordability" in stages


def test_loan_to_income_rejection(engine):
    rec = engine.submit("loan_application", loan(loan_amount=9000000)).record
    assert rec["status"] == "REJECTED"
    assert engine.explain(rec["id"])["reasons"][0].startswith("LOAN_TO_INCOME")


def test_second_workflow_runs_on_same_engine(engine):
    assert engine.submit("claim_processing", claim()).record["status"] == "APPROVED"
    assert engine.submit("claim_processing", claim(claim_id="CLM-2", claim_amount=70000)).record["status"] \
        == "MANUAL_REVIEW"
    assert engine.submit("claim_processing", claim(claim_id="CLM-3", policy_number="POL-SMALL")).record["status"] \
        == "REJECTED"


# ---- invalid input ------------------------------------------------------

def test_invalid_input_is_rejected_and_not_stored(engine):
    with pytest.raises(ValidationError) as exc:
        engine.submit("loan_application", loan(age="thirty", employment_status="pirate"))
    assert len(exc.value.errors) == 2
    assert engine.store.get_by_key("anything") is None


def test_unknown_workflow(engine):
    with pytest.raises(ConfigError):
        engine.submit("nope", {})


# ---- idempotency / duplicates ------------------------------------------

def test_duplicate_request_returns_same_record(engine):
    first = engine.submit("loan_application", loan(), idempotency_key="k-1")
    second = engine.submit("loan_application", loan(), idempotency_key="k-1")
    assert second.replayed
    assert second.record["id"] == first.record["id"]
    assert len(engine.history(first.record["id"])) == 5      # not processed twice


def test_duplicate_without_key_detected_by_payload_hash(engine):
    first = engine.submit("loan_application", loan())
    second = engine.submit("loan_application", loan())
    assert second.replayed and second.record["id"] == first.record["id"]


def test_same_key_different_payload_conflicts(engine):
    engine.submit("loan_application", loan(), idempotency_key="k-2")
    with pytest.raises(IdempotencyConflict):
        engine.submit("loan_application", loan(loan_amount=10), idempotency_key="k-2")


def test_dependency_not_called_again_on_replay(engine):
    engine.submit("loan_application", loan(applicant_id="APP-OK"), idempotency_key="k-3")
    engine.submit("loan_application", loan(applicant_id="APP-OK"), idempotency_key="k-3")
    assert engine.services["credit_bureau"].calls["APP-OK"] == 1


# ---- dependency failure and retry --------------------------------------

def test_transient_failures_are_retried_with_backoff(engine, sleeps):
    rec = engine.submit("loan_application", loan(applicant_id="APP-FLAKY-1")).record
    # FLAKY fails twice then succeeds; max_attempts is 3 so it recovers inside one run
    assert rec["status"] == "APPROVED" or rec["status"] == "MANUAL_REVIEW"
    assert sleeps == [0.2, 0.4]
    calls = engine.explain(rec["id"])["dependency_calls"]
    assert [c["outcome"] for c in calls] == ["transient_failure", "transient_failure", "success"]


def test_exhausted_retries_park_request_then_manual_retry_succeeds(engine):
    engine.services["credit_bureau"].flaky_failures = 4    # more than one run's 3 attempts
    rec = engine.submit("loan_application", loan(applicant_id="APP-FLAKY-2")).record
    assert rec["status"] == "RETRY_PENDING"
    assert rec["current_stage"] == "credit_check"

    rec = engine.retry(rec["id"])                          # 4th call fails, 5th succeeds
    assert rec["status"] == "APPROVED" or rec["status"] == "MANUAL_REVIEW"
    states = [h["to_status"] for h in engine.history(rec["id"])]
    assert "RETRY_PENDING" in states
    events = [a["event"] for a in engine.audit(rec["id"])]
    assert "dependency_exhausted" in events and "retry_started" in events


def test_retry_only_allowed_when_pending(engine):
    rec = engine.submit("loan_application", loan()).record
    with pytest.raises(InvalidOperation):
        engine.retry(rec["id"])


def test_dependency_down_falls_back_to_manual_review(engine):
    rec = engine.submit("claim_processing", claim(policy_number="POL-DOWN")).record
    assert rec["status"] == "MANUAL_REVIEW"
    reasons = engine.explain(rec["id"])["reasons"]
    assert reasons[0].startswith("DEPENDENCY_FAILURE")


def test_permanent_dependency_error_does_not_retry(engine, sleeps):
    rec = engine.submit("claim_processing", claim(policy_number="XYZ-999")).record
    assert rec["status"] == "MANUAL_REVIEW"
    assert sleeps == []
    assert len(engine.explain(rec["id"])["dependency_calls"]) == 1


# ---- partial save failure ----------------------------------------------

def test_failed_checkpoint_rolls_back_and_replay_resumes(engine):
    engine.store.inject_failures(1)
    # first checkpoint (RECEIVED -> IN_PROGRESS) fails half way through
    with pytest.raises(StoreError):
        engine.submit("loan_application", loan(), idempotency_key="k-save")

    stored = engine.store.get_by_key("k-save")
    assert stored["status"] == "RECEIVED"                  # state untouched
    assert len(engine.history(stored["id"])) == 1          # no half written history row
    assert [a["event"] for a in engine.audit(stored["id"])] == ["request_received"]

    result = engine.submit("loan_application", loan(), idempotency_key="k-save")
    assert result.replayed and result.record["id"] == stored["id"]
    assert result.record["status"] == "APPROVED"


# ---- manual review ------------------------------------------------------

def test_manual_review_decision_is_audited(engine):
    rec = engine.submit("loan_application", loan(applicant_id="APP-BORDER")).record
    assert rec["status"] == "MANUAL_REVIEW"
    rec = engine.review(rec["id"], "priya.k", "approve", "stable income, verified payslips")
    assert rec["status"] == "APPROVED"
    exp = engine.explain(rec["id"])
    assert "priya.k" in exp["reasons"][0]


def test_review_requires_reason_and_right_state(engine):
    done = engine.submit("loan_application", loan()).record
    with pytest.raises(InvalidOperation):
        engine.review(done["id"], "priya.k", "approve", "x")
    pending = engine.submit("loan_application", loan(applicant_id="APP-BORDER")).record
    with pytest.raises(InvalidOperation):
        engine.review(pending["id"], "priya.k", "approve", "")


def test_final_state_cannot_change(engine):
    rec = engine.submit("loan_application", loan()).record
    with pytest.raises(Exception):
        engine.store.checkpoint(rec["id"], status="REJECTED", stage="rejected",
                                context=rec["context"], events=[])


# ---- rule / config changes ---------------------------------------------

def _edit_loan_config(config_dir, mutate):
    path = config_dir / "loan_application.json"
    raw = json.loads(path.read_text())
    mutate(raw)
    path.write_text(json.dumps(raw))


def test_rule_change_applies_to_new_requests_only(engine, config_dir):
    old = engine.submit("loan_application", loan(applicant_id="APP-OK", loan_amount=800000)).record
    assert old["status"] == "APPROVED"                     # 800k <= 5 * 900k

    def tighten(raw):
        raw["version"] = 2
        rule = raw["stages"]["affordability"]["rules"][1]
        rule["when"]["value"]["multiply"] = 0.5            # now max half of income
    _edit_loan_config(config_dir, tighten)
    assert engine.registry.reload()["loan_application"] == 2

    new = engine.submit("loan_application", loan(applicant_id="APP-OK", loan_amount=800000,
                                                  full_name="Asha V")).record
    assert new["status"] == "REJECTED"
    assert new["workflow_version"] == 2
    assert engine.get(old["id"])["workflow_version"] == 1  # old decision still explains itself under v1
    assert engine.explain(old["id"])["outcome"] == "APPROVED"


def test_in_flight_request_keeps_its_workflow_version(engine, config_dir):
    engine.services["credit_bureau"].flaky_failures = 4
    parked = engine.submit("loan_application", loan(applicant_id="APP-FLAKY-3", loan_amount=800000)).record
    assert parked["status"] == "RETRY_PENDING"

    def tighten(raw):
        raw["version"] = 2
        raw["stages"]["affordability"]["rules"][1]["when"]["value"]["multiply"] = 0.5
    _edit_loan_config(config_dir, tighten)
    engine.registry.reload()

    done = engine.retry(parked["id"])
    assert done["workflow_version"] == 1
    assert done["status"] != "REJECTED" or "LOAN_TO_INCOME" not in engine.explain(done["id"])["reasons"][0]


def test_new_rule_added_through_config_only(engine, config_dir):
    def add_rule(raw):
        raw["version"] = 2
        raw["stages"]["mandatory_checks"]["rules"].append({
            "id": "BLOCKED_NAME", "description": "Blocklisted applicant",
            "when": {"field": "data.full_name", "op": "==", "value": "Ravi Blocked"},
            "goto": "rejected"})
    _edit_loan_config(config_dir, add_rule)
    engine.registry.reload()
    rec = engine.submit("loan_application", loan(full_name="Ravi Blocked")).record
    assert rec["status"] == "REJECTED"
    assert engine.explain(rec["id"])["reasons"] == ["BLOCKED_NAME: Blocklisted applicant"]


def test_bad_config_edit_is_refused_and_old_config_kept(engine, config_dir):
    _edit_loan_config(config_dir, lambda raw: raw["stages"]["mandatory_checks"].update(default="missing"))
    with pytest.raises(ConfigError):
        engine.registry.reload()
    assert engine.submit("loan_application", loan()).record["status"] == "APPROVED"


def test_unknown_request_id(engine):
    with pytest.raises(NotFound):
        engine.get("nope")
