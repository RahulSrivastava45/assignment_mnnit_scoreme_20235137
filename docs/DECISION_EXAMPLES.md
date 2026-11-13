# Decision explanation examples

All outputs below were produced by `python -m decision_system explain <id>` against
the sample payloads in `samples/` and the shipped configs. Request ids differ per run.

## 1. Approved loan (`samples/loan_good.json`)

Input: age 32, income 900000, loan 800000, employed, applicant `APP-GOOD`.

```
Request 013302bb9255 (loan_application v1) is APPROVED.
Input: {"age": 32, "annual_income": 900000, "applicant_id": "APP-GOOD", "employment_status": "employed", "full_name": "Asha Verma", "loan_amount": 800000}
[mandatory_checks] MIN_AGE not matched - Applicant must be at least 18 (data.age=32 < 18 -> False)
[mandatory_checks] NO_INCOME not matched - ... (data.employment_status='employed' == 'unemployed' -> False; data.annual_income=900000 < 10000 -> False)
[mandatory_checks] no rule matched, default -> credit_check
[credit_check] credit_bureau attempt 1: ok -> {"bureau": "SIM-BUREAU", "score": 780}
[affordability] LOW_CREDIT not matched - Credit score below 550 is rejected (credit.score=780 < 550 -> False)
[affordability] LOAN_TO_INCOME not matched - Loan may not exceed 5x annual income (data.loan_amount=800000 > 4500000 -> False)
[affordability] BORDERLINE_CREDIT not matched - ... (credit.score=780 < 650 -> False)
[affordability] LARGE_LOAN not matched - ... (data.loan_amount=800000 > 1000000 -> False)
[affordability] no rule matched, default -> approved
Decision: APPROVED
  because no rule triggered a rejection or review; default path taken
```

Audit reasoning: every rejection/review rule was evaluated against a concrete data
reference (`data.*` is the request, `credit.*` is the bureau response) and none fired.

## 2. Rejected on a mandatory check (`samples/loan_underage.json`)

```
[mandatory_checks] MIN_AGE MATCHED - Applicant must be at least 18 (data.age=17 < 18 -> True)
Decision: REJECTED
  because MIN_AGE: Applicant must be at least 18
```

The credit bureau is never called: the rule short-circuits to `rejected`, so no
external cost is spent on a request that cannot be approved.

## 3. Multi-step evaluation ending in manual review, then a human decision (`samples/loan_borderline.json`)

```
[affordability] LOW_CREDIT not matched - ... (credit.score=600 < 550 -> False)
[affordability] LOAN_TO_INCOME not matched - ... (data.loan_amount=900000 > 3500000 -> False)
[affordability] BORDERLINE_CREDIT MATCHED - Credit score between 550 and 649 needs a human look (credit.score=600 < 650 -> True)
Decision: MANUAL_REVIEW
  because BORDERLINE_CREDIT: Credit score between 550 and 649 needs a human look
```

After `review <id> approve --reviewer priya.k --reason "stable income, payslips verified"`:

```
Decision: APPROVED
  because manual review by priya.k: stable income, payslips verified
```

The `audit` trail keeps both the system's `MANUAL_REVIEW` decision and the human
override (actor, decision, reason); `history` shows `MANUAL_REVIEW -> APPROVED`.

## 4. Flaky dependency recovered by retries (`samples/loan_flaky_bureau.json`)

```
[credit_check] credit_bureau attempt 1: transient_failure (credit_bureau timed out (call 1))
[credit_check] credit_bureau attempt 2: transient_failure (credit_bureau timed out (call 2))
[credit_check] credit_bureau attempt 3: ok -> {"bureau": "SIM-BUREAU", "score": 585}
```

Backoff between attempts was 0.2s then 0.4s. The decision then continued normally
using the score that finally came back.

## 5. Dependency down, fallback to manual review (claim with `POL-DOWN`)

```
[policy_lookup] policy_service attempt 1: transient_failure (policy_service unavailable)
[policy_lookup] policy_service attempt 2: transient_failure (policy_service unavailable)
Decision: MANUAL_REVIEW
  because DEPENDENCY_FAILURE: policy_service: dependency still failing after 2 attempts, falling back to manual_review
```

The system does not guess when it lacks data: it routes to a human and records why.
(For the loan workflow, the same situation parks the request as `RETRY_PENDING`
instead, because that workflow sets `"on_exhausted": "retry_later"`.)

## 6. Over coverage limit (`samples/claim_over_limit.json`)

```
[policy_lookup] policy_service attempt 1: ok -> {"active": true, "coverage_limit": 5000}
[coverage] POLICY_INACTIVE not matched - Policy must be active (policy.active=True == False -> False)
[coverage] OVER_LIMIT MATCHED - Claim exceeds the coverage limit of the policy (data.claim_amount=9000 > 5000 -> True)
Decision: REJECTED
  because OVER_LIMIT: Claim exceeds the coverage limit of the policy
```

Here the right-hand side of the comparison is itself a data reference
(`policy.coverage_limit`), shown resolved in the trace.

## Machine readable form

`GET /requests/<id>/explanation` returns the same information as JSON
(`input`, `outcome`, `reasons`, `rule_trace`, `dependency_calls`, `summary`), and
`GET /requests/<id>/audit` returns the raw append-only events.
