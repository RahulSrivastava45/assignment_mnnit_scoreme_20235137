"""Simulated external services.

The engine only knows the `call(params)` interface, so swapping these for real
HTTP clients later doesn't touch workflow code.

Test-friendly behaviour is keyed off the id passed in:
    contains "FLAKY" -> first 2 calls for that id fail, then it works
    contains "DOWN"  -> always unavailable
"""
import zlib


class TransientDependencyError(Exception):
    """Temporary failure (timeout, 503). Worth retrying."""


class PermanentDependencyError(Exception):
    """The service answered but the request can never succeed."""


class SimulatedService:
    def __init__(self, name, flaky_failures=2):
        self.name = name
        self.flaky_failures = flaky_failures
        self.calls = {}  # key -> number of calls seen

    def _guard(self, key):
        self.calls[key] = self.calls.get(key, 0) + 1
        if "DOWN" in key:
            raise TransientDependencyError(f"{self.name} unavailable")
        if "FLAKY" in key and self.calls[key] <= self.flaky_failures:
            raise TransientDependencyError(f"{self.name} timed out (call {self.calls[key]})")

    def call(self, params):
        raise NotImplementedError


class CreditBureau(SimulatedService):
    # fixed answers for a few ids so demos are predictable
    KNOWN = {"APP-GOOD": 780, "APP-OK": 700, "APP-BORDER": 600, "APP-POOR": 480}

    def __init__(self):
        super().__init__("credit_bureau")

    def call(self, params):
        applicant = params["applicant_id"]
        self._guard(applicant)
        score = self.KNOWN.get(applicant)
        if score is None:
            # stable pseudo score in 300..850 for any other id
            score = 300 + zlib.crc32(applicant.encode()) % 551
        return {"score": score, "bureau": "SIM-BUREAU"}


class PolicyService(SimulatedService):
    KNOWN = {
        "POL-1001": {"active": True, "coverage_limit": 100000},
        "POL-SMALL": {"active": True, "coverage_limit": 5000},
        "POL-LAPSED": {"active": False, "coverage_limit": 100000},
    }

    def __init__(self):
        super().__init__("policy_service")

    def call(self, params):
        number = params["policy_number"]
        self._guard(number)
        if number in self.KNOWN:
            return dict(self.KNOWN[number])
        if number.startswith("POL-"):
            return {"active": True, "coverage_limit": 25000}
        raise PermanentDependencyError(f"policy {number} not found")


def default_services():
    return {s.name: s for s in (CreditBureau(), PolicyService())}
