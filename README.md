# Resilient Decision System

A configurable workflow decision platform. It accepts structured requests,
validates them, evaluates rules, runs workflow stages (including calls to an
external dependency), tracks state, and records an audit trail that explains every
decision. Workflows and rules live in JSON, so a business change is a config edit.

Two example workflows ship in `config/workflows/`:

* `loan_application`: age/income checks, credit bureau lookup, affordability, manual review
* `claim_processing`: document completeness, policy lookup, coverage checks

Pure Python 3.9+, standard library only (pytest for the tests).

## Setup

```bash
git clone <this repo>
cd resilient-decision-system
python -m venv .venv
.venv\Scripts\activate        # Windows  (source .venv/bin/activate on Linux/Mac)
pip install -r requirements.txt
```

## Run the tests

```bash
python -m pytest -q
```

## Use it

### CLI

```bash
python -m decision_system submit loan_application samples/loan_good.json
python -m decision_system explain <request_id>
python -m decision_system history <request_id>
python -m decision_system audit <request_id>
python -m decision_system review <request_id> approve --reviewer priya.k --reason "payslips verified"
python -m decision_system retry <request_id>
python -m decision_system submit loan_application samples/loan_good.json --key my-key-1   # idempotency key
```

State is kept in `data/decisions.db` (override with `--db`).

### REST API

```bash
python -m decision_system serve --port 8000
```

```bash
curl -X POST localhost:8000/requests -H "Idempotency-Key: demo-1" -H "Content-Type: application/json" \
     -d '{"workflow": "loan_application", "data": {"applicant_id": "APP-GOOD", "full_name": "Asha Verma", "age": 32, "annual_income": 900000, "loan_amount": 800000, "employment_status": "employed"}}'
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/requests` | Submit (201 new, 200 duplicate replay, 422 invalid, 409 key reused with other data) |
| GET | `/requests/<id>` | Current state |
| GET | `/requests/<id>/history` | Full state change history |
| GET | `/requests/<id>/audit` | Audit events (rules, data refs, dependency calls) |
| GET | `/requests/<id>/explanation` | Readable decision explanation |
| POST | `/requests/<id>/retry` | Resume a `RETRY_PENDING` request |
| POST | `/requests/<id>/review` | Human decision for `MANUAL_REVIEW` |
| GET | `/workflows` | Configured workflows |
| POST | `/admin/reload` | Re-read config files (validated before swap) |

A storage failure returns `503` with `"retryable": true`; resend with the same key.

## Test data

Simulated dependencies react to the ids in the request:

| Id contains / equals | Behaviour |
|---|---|
| `APP-GOOD`, `APP-OK`, `APP-BORDER`, `APP-POOR` | credit score 780, 700, 600, 480 |
| `FLAKY` | first 2 calls to the service time out, then it works |
| `DOWN` | service always unavailable |
| `POL-SMALL` / `POL-LAPSED` / `POL-1001` | coverage limit 5000 / inactive policy / normal |

## Configuration model

A workflow is one JSON file. Trimmed example:

```json
{
  "name": "loan_application",
  "version": 1,
  "schema": {"fields": {"age": {"type": "integer", "required": true, "min": 0}}},
  "start": "mandatory_checks",
  "stages": {
    "mandatory_checks": {
      "type": "rules",
      "rules": [
        {"id": "MIN_AGE", "description": "Applicant must be at least 18",
         "when": {"field": "data.age", "op": "<", "value": 18}, "goto": "rejected"}
      ],
      "default": "credit_check"
    },
    "credit_check": {
      "type": "dependency", "service": "credit_bureau",
      "input": {"applicant_id": "data.applicant_id"}, "store_as": "credit",
      "max_attempts": 3, "backoff_seconds": 0.2,
      "on_success": "affordability", "on_failure": "manual_review",
      "on_exhausted": "retry_later"
    },
    "approved": {"type": "terminal", "status": "APPROVED"}
  }
}
```

* **Stage types:** `rules`, `dependency`, `terminal`.
* **Conditions:** `{"field", "op", "value"}` with `== != > >= < <= in not_in contains exists`; combine with `all` / `any` / `not`; values can reference another field: `{"ref": "data.annual_income", "multiply": 5}`.
* **Paths:** `data.*` is the request, other prefixes are the `store_as` name of a dependency response.
* **On dependency failure:** `on_failure` (fall back to another stage) and optionally `"on_exhausted": "retry_later"` (park as `RETRY_PENDING`).

### Changing a rule without touching code

1. Edit `config/workflows/loan_application.json` (e.g. change `multiply: 5` to `3`, bump `version` to `2`).
2. `POST /admin/reload` (or restart the CLI/server).
3. New requests use version 2. Existing requests keep the version they started with, so old decisions still explain correctly. An invalid edit is refused and the previous config stays live.

## Design notes

* **Idempotency:** `Idempotency-Key` header/field, or a hash of workflow + payload if none is given. Same key + same payload returns the stored result without re-running anything; same key + different payload is a 409.
* **Consistency:** each step's state change, history row and audit events commit in one SQLite transaction. A failed save leaves the previous checkpoint intact and a resubmit resumes from there.
* **Retries:** transient dependency errors retry with exponential backoff; permanent ones do not.
* **Explainability:** every evaluated rule is audited with the exact field, operator, expected and actual values.

## Documentation

* [Architecture](docs/ARCHITECTURE.md): components, data flow, failure handling, trade-offs, assumptions, scaling
* [Decision examples](docs/DECISION_EXAMPLES.md): input, rules triggered, output and audit reasoning

## Project layout

```
config/workflows/      workflow + rule definitions (JSON)
decision_system/       engine, rules, schema, store, dependencies, api, cli
docs/                  architecture + explanation examples
samples/               example request payloads
tests/                 pytest suite
```

## Tests cover

happy path, invalid input, duplicate requests, same-key conflicts, dependency
failure + fallback, retry flow with backoff, partial save failure and resume,
manual review, lifecycle rules, rule/config changes (new version, in-flight
requests, bad edits), and the REST API.
