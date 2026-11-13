"""The decision engine: intake, idempotency, stage execution, retries, review."""
import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict

from .dependencies import PermanentDependencyError, TransientDependencyError
from .rules import evaluate, resolve, MISSING
from .schema import validate
from .workflow import WorkflowDefinition, APPROVED, REJECTED, IN_PROGRESS, RECEIVED, RETRY_PENDING, MANUAL_REVIEW
from .store import DuplicateKey


class IdempotencyConflict(Exception):
    """Same idempotency key used with a different payload."""


class NotFound(Exception):
    pass


class InvalidOperation(Exception):
    pass


class SubmitResult:
    def __init__(self, record, replayed):
        self.record = record
        self.replayed = replayed


def payload_hash(workflow, data):
    canonical = json.dumps({"workflow": workflow, "data": data}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class DecisionEngine:
    MAX_STEPS = 50  # guards against a config that loops forever

    def __init__(self, store, registry, services, sleep=time.sleep):
        self.store = store
        self.registry = registry
        self.services = services
        self.sleep = sleep
        self._locks = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()

    def _lock_for(self, request_id):
        with self._locks_guard:
            return self._locks[request_id]

    # -- public API -------------------------------------------------------

    def submit(self, workflow, data, idempotency_key=None):
        definition = self.registry.get(workflow)
        validate(data, definition.schema)

        digest = payload_hash(workflow, data)
        key = idempotency_key or f"auto:{digest}"

        existing = self.store.get_by_key(key)
        if existing:
            return self._replay(existing, digest)

        request_id = uuid.uuid4().hex[:12]
        try:
            record = self.store.create_request(request_id, key, digest, definition, data)
        except DuplicateKey:
            # lost a race with a concurrent identical submit
            return self._replay(self.store.get_by_key(key), digest)

        return SubmitResult(self._execute(record["id"]), False)

    def retry(self, request_id):
        record = self._require(request_id)
        if record["status"] != RETRY_PENDING:
            raise InvalidOperation(f"request is {record['status']}, only RETRY_PENDING can be retried")
        return self._execute(request_id)

    def review(self, request_id, reviewer, decision, reason):
        if decision not in ("approve", "reject"):
            raise InvalidOperation("decision must be 'approve' or 'reject'")
        if not reviewer or not reason:
            raise InvalidOperation("reviewer and reason are required")

        with self._lock_for(request_id):
            record = self._require(request_id)
            if record["status"] != MANUAL_REVIEW:
                raise InvalidOperation(f"request is {record['status']}, not awaiting manual review")
            status = APPROVED if decision == "approve" else REJECTED
            stage = record["current_stage"]
            events = [
                {"event": "manual_review_decision", "stage": stage, "reviewer": reviewer,
                 "decision": decision, "reason": reason},
                {"event": "decision", "stage": stage, "outcome": status,
                 "reasons": [f"manual review by {reviewer}: {reason}"],
                 "workflow_version": record["workflow_version"]},
            ]
            return self.store.checkpoint(request_id, status=status, stage=stage,
                                         context=record["context"], events=events,
                                         note=f"manual review: {decision}")

    def get(self, request_id):
        return self._require(request_id)

    def history(self, request_id):
        self._require(request_id)
        return self.store.history(request_id)

    def audit(self, request_id):
        self._require(request_id)
        return self.store.audit(request_id)

    def explain(self, request_id):
        record = self._require(request_id)
        audit = self.store.audit(request_id)

        rule_trace, dependency_calls, decision = [], [], None
        for item in audit:
            d = dict(item["detail"], stage=item["stage"])
            if item["event"] in ("rule_evaluated", "default_applied"):
                rule_trace.append(dict(d, event=item["event"]))
            elif item["event"] == "dependency_call":
                dependency_calls.append(d)
            elif item["event"] == "decision":
                decision = d

        return {
            "request_id": request_id,
            "workflow": record["workflow"],
            "workflow_version": record["workflow_version"],
            "status": record["status"],
            "input": record["payload"],
            "outcome": decision["outcome"] if decision else None,
            "reasons": decision["reasons"] if decision else [],
            "rule_trace": rule_trace,
            "dependency_calls": dependency_calls,
            "summary": self._summary(record, rule_trace, dependency_calls, decision),
        }

    # -- internals --------------------------------------------------------

    def _require(self, request_id):
        record = self.store.get(request_id)
        if record is None:
            raise NotFound(request_id)
        return record

    def _replay(self, existing, digest):
        if existing["payload_hash"] != digest:
            raise IdempotencyConflict(
                f"idempotency key '{existing['idempotency_key']}' was already used with different data")
        if existing["status"] in (RECEIVED, IN_PROGRESS):
            # earlier attempt died part way (e.g. failed save); pick it up again
            return SubmitResult(self._execute(existing["id"]), True)
        return SubmitResult(existing, True)

    def _execute(self, request_id):
        with self._lock_for(request_id):
            record = self._require(request_id)
            if record["status"] not in (RECEIVED, IN_PROGRESS, RETRY_PENDING):
                return record

            definition = WorkflowDefinition(record["workflow_snapshot"])
            context = record["context"]
            stage_name = record["current_stage"]

            if record["status"] != IN_PROGRESS:
                event = "workflow_started" if record["status"] == RECEIVED else "retry_started"
                record = self.store.checkpoint(
                    request_id, status=IN_PROGRESS, stage=stage_name, context=context,
                    events=[{"event": event, "stage": stage_name}], note=event)

            for _ in range(self.MAX_STEPS):
                stage = definition.stages[stage_name]
                if stage["type"] == "terminal":
                    return self._finish(record, stage_name, stage, context, [])

                full = {"data": record["payload"], **context}
                if stage["type"] == "rules":
                    nxt, events = self._run_rules(stage_name, stage, full, context)
                else:
                    nxt, events = self._run_dependency(stage_name, stage, full, context)

                if nxt is None:
                    return self.store.checkpoint(
                        request_id, status=RETRY_PENDING, stage=stage_name, context=context,
                        events=events, note="dependency unavailable, waiting for retry")

                target = definition.stages[nxt]
                if target["type"] == "terminal":
                    return self._finish(record, nxt, target, context, events)

                record = self.store.checkpoint(
                    request_id, status=IN_PROGRESS, stage=nxt, context=context,
                    events=events, note=f"{stage_name} -> {nxt}")
                stage_name = nxt

            raise InvalidOperation(f"workflow exceeded {self.MAX_STEPS} steps, check config for loops")

    def _finish(self, record, stage_name, stage, context, events):
        trail = context.get("_trail", [])
        reasons = [f"{t['rule']}: {t['description']}" for t in trail] or \
            ["no rule triggered a rejection or review; default path taken"]
        events = list(events) + [{
            "event": "decision", "stage": stage_name, "outcome": stage["status"],
            "reasons": reasons, "workflow_version": record["workflow_version"],
        }]
        return self.store.checkpoint(
            record["id"], status=stage["status"], stage=stage_name, context=context,
            events=events, note=f"decision: {stage['status']}")

    def _run_rules(self, stage_name, stage, full, context):
        events = []
        for rule in stage.get("rules", []):
            trace = []
            matched = evaluate(rule["when"], full, trace)
            event = {"event": "rule_evaluated", "stage": stage_name, "rule": rule["id"],
                     "description": rule.get("description", ""), "matched": matched,
                     "conditions": trace}
            if matched:
                event["goto"] = rule["goto"]
                events.append(event)
                context.setdefault("_trail", []).append({
                    "stage": stage_name, "rule": rule["id"],
                    "description": rule.get("description", "")})
                return rule["goto"], events
            events.append(event)
        events.append({"event": "default_applied", "stage": stage_name, "goto": stage["default"]})
        return stage["default"], events

    def _run_dependency(self, stage_name, stage, full, context):
        """Returns (next_stage, events). next_stage None means 'park for retry'."""
        events = []
        service = self.services.get(stage["service"])
        params = {}
        for name, path in stage.get("input", {}).items():
            value = resolve(path, full)
            params[name] = None if value is MISSING else value

        attempts = stage.get("max_attempts", 3)
        delay = stage.get("backoff_seconds", 0.1)
        permanent = service is None
        if service is None:
            events.append({"event": "dependency_call", "stage": stage_name, "service": stage["service"],
                           "attempt": 1, "outcome": "permanent_failure", "error": "service not configured"})

        for attempt in range(1, attempts + 1):
            if permanent:
                break
            try:
                result = service.call(params)
            except TransientDependencyError as exc:
                events.append({"event": "dependency_call", "stage": stage_name, "service": stage["service"],
                               "attempt": attempt, "outcome": "transient_failure", "error": str(exc)})
                if attempt < attempts:
                    self.sleep(delay * 2 ** (attempt - 1))
                continue
            except PermanentDependencyError as exc:
                events.append({"event": "dependency_call", "stage": stage_name, "service": stage["service"],
                               "attempt": attempt, "outcome": "permanent_failure", "error": str(exc)})
                permanent = True
                break
            events.append({"event": "dependency_call", "stage": stage_name, "service": stage["service"],
                           "attempt": attempt, "outcome": "success", "request": params, "response": result})
            context[stage.get("store_as", stage["service"])] = result
            return stage["on_success"], events

        if not permanent and stage.get("on_exhausted") == "retry_later":
            events.append({"event": "dependency_exhausted", "stage": stage_name, "service": stage["service"],
                           "attempts": attempts, "action": "retry_later"})
            return None, events

        reason = "dependency failed permanently" if permanent else f"dependency still failing after {attempts} attempts"
        context.setdefault("_trail", []).append({
            "stage": stage_name, "rule": "DEPENDENCY_FAILURE",
            "description": f"{stage['service']}: {reason}, falling back to {stage['on_failure']}"})
        events.append({"event": "dependency_fallback", "stage": stage_name, "service": stage["service"],
                       "goto": stage["on_failure"], "reason": reason})
        return stage["on_failure"], events

    # -- explanation text -------------------------------------------------

    @staticmethod
    def _fmt_cond(c):
        if c.get("missing"):
            return f"{c['field']} is missing -> False"
        return f"{c['field']}={c['actual']!r} {c['op']} {c['expected']!r} -> {c['result']}"

    def _summary(self, record, rule_trace, dependency_calls, decision):
        lines = [f"Request {record['id']} ({record['workflow']} v{record['workflow_version']}) "
                 f"is {record['status']}."]
        lines.append(f"Input: {json.dumps(record['payload'], sort_keys=True)}")
        for call in dependency_calls:
            if call["outcome"] == "success":
                lines.append(f"[{call['stage']}] {call['service']} attempt {call['attempt']}: "
                             f"ok -> {json.dumps(call['response'], sort_keys=True)}")
            else:
                lines.append(f"[{call['stage']}] {call['service']} attempt {call['attempt']}: "
                             f"{call['outcome']} ({call['error']})")
        for step in rule_trace:
            if step["event"] == "default_applied":
                lines.append(f"[{step['stage']}] no rule matched, default -> {step['goto']}")
                continue
            verdict = "MATCHED" if step["matched"] else "not matched"
            checks = "; ".join(self._fmt_cond(c) for c in step["conditions"])
            lines.append(f"[{step['stage']}] {step['rule']} {verdict} - {step['description']} ({checks})")
        if decision:
            lines.append(f"Decision: {decision['outcome']}")
            for reason in decision["reasons"]:
                lines.append(f"  because {reason}")
        else:
            lines.append("No final decision yet.")
        return "\n".join(lines)
