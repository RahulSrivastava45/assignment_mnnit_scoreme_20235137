# Architecture

## 1. Problem and boundaries

The platform accepts a structured request for a named workflow, runs it through
configured stages (rules, external lookups), records every step, and ends in one
of `APPROVED`, `REJECTED` or `MANUAL_REVIEW` (or parks in `RETRY_PENDING` when a
dependency is down).

What is in scope: intake and validation, rule evaluation, stage execution, state
tracking, audit, retries, idempotency, configuration. What is out of scope:
authentication, a UI, a real message queue, real third-party integrations
(simulated here).

The key boundary is **engine vs. configuration**. The engine knows how to run
three kinds of stage. Everything business specific (fields, thresholds, stage
order, which service to call, where to go on failure) is in JSON.

## 2. Components

```
            +-----------+     +-----------+
  clients ->|  REST API |     |    CLI    |
            +-----+-----+     +-----+-----+
                  |                 |
                  +--------+--------+
                           v
                  +-----------------+       +--------------------+
                  |  DecisionEngine |------>| WorkflowRegistry   |  JSON configs
                  |  (engine.py)    |       | (workflow.py)      |
                  +--+-----+-----+--+       +--------------------+
                     |     |     |
          validate   |     |     |  call()
        +------------+     |     +--------------+
        v                  v                    v
  schema.py           rules.py            dependencies.py
  (input checks)      (conditions +       (simulated credit
                       trace)              bureau, policy svc)
                     |
                     v  checkpoint() = one transaction
                  +-----------------+
                  |  Store (SQLite) |  requests / history / audit
                  +-----------------+
```

| Module | Responsibility | Depends on |
|---|---|---|
| `schema.py` | Validate request payload against the workflow's schema block | nothing |
| `rules.py` | Evaluate JSON conditions, emit a per-check trace | nothing |
| `workflow.py` | Parse + validate configs, lifecycle states and allowed transitions | nothing |
| `dependencies.py` | Simulated external services behind a `call(params)` interface | nothing |
| `store.py` | Persistence and atomic state changes | `workflow.py` (transition rule) |
| `engine.py` | Orchestration, idempotency, retries, review, explanations | all of the above |
| `api.py` / `cli.py` | Transport only; translate to engine calls and errors to codes | engine |

The leaf modules do not import each other, so each can be tested alone (see
`tests/test_schema_and_rules.py`).

## 3. Data flow

1. `submit(workflow, data, key)`
2. Load the workflow definition, validate `data` against its schema (422 on failure; nothing stored).
3. Compute `sha256(workflow + canonical data)`. Idempotency key = supplied key, or `auto:<hash>`.
4. Look the key up. Found and same hash: replay. Found and different hash: conflict.
5. Insert the request (`RECEIVED`), a history row and a `request_received` audit event in one transaction. The workflow definition is **snapshotted into the row**.
6. Engine loop, one stage at a time, each followed by a `checkpoint()`:
   * `rules` stage: evaluate rules in order; first match picks the next stage; otherwise `default`. Every rule evaluated is audited with its leaf checks (field path, operator, expected, actual, result).
   * `dependency` stage: call the service, retry transient errors with exponential backoff; on success store the response in the context; on failure either park (`RETRY_PENDING`) or follow `on_failure`.
   * `terminal` stage: set the final status and write a `decision` audit event with reasons.
7. Return the record. `explain()` rebuilds the human-readable story from the audit rows.

## 4. State management

```
RECEIVED -> IN_PROGRESS -> APPROVED
                |      \-> REJECTED
                |      \-> MANUAL_REVIEW -> APPROVED | REJECTED   (human decision)
                \--> RETRY_PENDING -> IN_PROGRESS                 (explicit retry)
```

Transitions are enforced in `checkpoint()` using `ALLOWED_TRANSITIONS`.
`APPROVED` and `REJECTED` are final. Every transition writes a `history` row
(from/to status and stage, timestamp, note), so the full lifecycle can be replayed.

## 5. Failure handling

| Failure | Handling |
|---|---|
| Bad input | Rejected up front with a list of all field errors; nothing persisted |
| Duplicate request | Idempotency key (or payload hash) returns the stored record; no re-execution, no second dependency call |
| Same key, different data | `409` conflict, existing record untouched |
| Transient dependency error | Retried inside the stage with exponential backoff (`backoff_seconds * 2^n`), up to `max_attempts`; every attempt audited |
| Retries exhausted | Config choice per stage: park as `RETRY_PENDING` (resume via `/retry`) or fall back to another stage (e.g. manual review) |
| Permanent dependency error | No retry; follows `on_failure` |
| Save failure mid-step | `checkpoint()` is one transaction: state, history and audit commit together or not at all. API returns `503 retryable`. Re-sending the same key finds the request still `RECEIVED`/`IN_PROGRESS` and resumes from the last checkpoint |
| Concurrent identical submits | Unique index on the key; the loser catches the integrity error and replays. A per-request lock prevents two threads executing the same request |
| Config typo | `reload()` validates everything before swapping; a bad file leaves the old config active |
| Config loop | `MAX_STEPS` guard |

Stages are safe to re-run: rule evaluation is pure and a repeated dependency
call is a read. Checkpoints happen *after* a stage completes, so a crash replays
at most the stage that was in flight.

## 6. Configuration model

See the README for a worked example. A workflow is one JSON file:

* `schema`: field types, required, min/max, enum, min_length
* `start`: first stage
* `stages`: map of name to `rules` | `dependency` | `terminal`
* rule conditions: `field op value`, composed with `all` / `any` / `not`; values can reference other fields (`{"ref": "data.income", "multiply": 5}`)

Adding a rule, changing a threshold, reordering stages or adding a whole new
workflow needs no Python change. New *kinds* of stage (say, a parallel
approval) need a new handler in the engine, which is the intended extension point.

**Versioning.** A request stores the exact definition it started with. Editing a
config and calling `/admin/reload` affects new requests only. In-flight and
finished requests keep running and explaining under the version they were
decided with, which is what makes old decisions auditable after rules change.

## 7. Auditability

Two append-only tables:

* `history`: state changes (what, when, from, to, note)
* `audit`: decision evidence (rules evaluated with data references, dependency calls with request and response, fallbacks, manual review actor and reason, final decision with reasons and workflow version)

Rows are only inserted, never updated. `explain()` is derived purely from them.

## 8. Trade-offs and assumptions

| Decision | Why | Cost |
|---|---|---|
| SQLite | Zero setup, real transactions, runnable anywhere | Single writer; swap for Postgres to scale writes |
| Synchronous execution | Simple to reason about, deterministic tests | A slow dependency holds the request thread. Mitigated by bounded retries and the park-and-retry path |
| JSON config + small condition language | Reviewable, diffable, no code execution | Less expressive than a scripting language; deliberately no `eval` |
| First matching rule wins | Predictable, easy to explain | Rule order matters; the audit shows all evaluated rules |
| Snapshot workflow per request | Stable, explainable history | Some storage duplication |
| Retry is explicit after parking | Operators stay in control | A scheduler could call `/retry` automatically (see scaling) |
| Standard library HTTP server | No dependencies | Not production grade; put behind a real server in deployment |
| Failed-validation requests are not stored | Keeps the audit log about real requests | Rejected garbage is not traceable; would log at the gateway |

Assumptions: requests are small JSON documents; a caller can supply a stable
idempotency key (otherwise the payload hash is used, which treats identical
payloads as the same request); external lookups are read-only so repeating them is safe;
timestamps are UTC.

## 9. Scaling considerations

* **Stateless workers.** All state is in the store, so engine processes can be replicated behind a load balancer. The per-request lock should become a DB-level lock or optimistic version column (`UPDATE ... WHERE version = ?`).
* **Database.** Move to PostgreSQL; the unique index on `idempotency_key` keeps duplicate protection correct across workers. Partition `audit`/`history` by time and archive old rows. Indexes on `(request_id, seq)` already serve the read paths.
* **Async execution.** Put accepted requests on a queue (SQS, Kafka, RabbitMQ). The API returns `202` with the id, workers pick up and run `_execute`; stages already checkpoint so workers can crash and resume.
* **Automatic retries.** A scheduler scans `RETRY_PENDING` and re-enqueues with jittered backoff.
* **Dependencies.** Add per-service circuit breakers, timeouts and rate limits so a failing vendor does not exhaust workers. Cache read-only lookups with a TTL.
* **Config distribution.** Store configs in a database or config service with versioned, approved rollouts; `reload()` becomes a pub/sub event.
* **Hot keys.** Duplicate bursts hit the same row; the unique index serialises them cheaply.
* **Observability.** Export per-stage latency, retry counts, queue depth, and counts by outcome; correlate by request id.
