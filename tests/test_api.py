import json
import threading
import urllib.error
import urllib.request

import pytest

from decision_system.api import serve

from conftest import loan


@pytest.fixture
def base_url(engine):
    server = serve(engine, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read()), dict(err.headers)


def test_submit_and_fetch(base_url):
    status, body, _ = call(base_url, "POST", "/requests", {"workflow": "loan_application", "data": loan()})
    assert status == 201 and body["status"] == "APPROVED"

    status, got, _ = call(base_url, "GET", f"/requests/{body['id']}")
    assert status == 200 and got["id"] == body["id"]

    status, exp, _ = call(base_url, "GET", f"/requests/{body['id']}/explanation")
    assert status == 200 and exp["outcome"] == "APPROVED"

    status, hist, _ = call(base_url, "GET", f"/requests/{body['id']}/history")
    assert hist["history"][-1]["to_status"] == "APPROVED"


def test_invalid_input_gives_422(base_url):
    status, body, _ = call(base_url, "POST", "/requests",
                           {"workflow": "loan_application", "data": loan(age="x")})
    assert status == 422 and body["details"]


def test_duplicate_via_header_returns_200_and_replay_flag(base_url):
    payload = {"workflow": "loan_application", "data": loan()}
    hdr = {"Idempotency-Key": "api-1"}
    s1, b1, _ = call(base_url, "POST", "/requests", payload, hdr)
    s2, b2, h2 = call(base_url, "POST", "/requests", payload, hdr)
    assert (s1, s2) == (201, 200)
    assert b1["id"] == b2["id"] and h2["Idempotent-Replay"] == "true"


def test_key_reuse_with_other_data_is_409(base_url):
    hdr = {"Idempotency-Key": "api-2"}
    call(base_url, "POST", "/requests", {"workflow": "loan_application", "data": loan()}, hdr)
    status, _, _ = call(base_url, "POST", "/requests",
                        {"workflow": "loan_application", "data": loan(age=50)}, hdr)
    assert status == 409


def test_retry_and_review_endpoints(base_url, engine):
    engine.services["credit_bureau"].flaky_failures = 4
    _, rec, _ = call(base_url, "POST", "/requests",
                     {"workflow": "loan_application", "data": loan(applicant_id="APP-FLAKY-9")})
    assert rec["status"] == "RETRY_PENDING"

    status, rec, _ = call(base_url, "POST", f"/requests/{rec['id']}/retry")
    assert status == 200 and rec["status"] != "RETRY_PENDING"

    _, border, _ = call(base_url, "POST", "/requests",
                        {"workflow": "loan_application", "data": loan(applicant_id="APP-BORDER")})
    status, done, _ = call(base_url, "POST", f"/requests/{border['id']}/review",
                           {"reviewer": "sam", "decision": "reject", "reason": "thin file"})
    assert status == 200 and done["status"] == "REJECTED"


def test_storage_failure_returns_503_and_retry_with_same_key_works(base_url, engine):
    payload = {"workflow": "loan_application", "data": loan()}
    hdr = {"Idempotency-Key": "api-3"}
    engine.store.inject_failures(1)
    status, body, _ = call(base_url, "POST", "/requests", payload, hdr)
    assert status == 503 and body["retryable"]
    status, body, _ = call(base_url, "POST", "/requests", payload, hdr)
    assert body["status"] == "APPROVED"


def test_unknown_things(base_url):
    assert call(base_url, "GET", "/requests/doesnotexist")[0] == 404
    assert call(base_url, "GET", "/nothing")[0] == 404
    assert call(base_url, "POST", "/requests", {"workflow": "x", "data": {}})[0] == 400
    assert call(base_url, "GET", "/workflows")[1]["workflows"] == ["claim_processing", "loan_application"]
