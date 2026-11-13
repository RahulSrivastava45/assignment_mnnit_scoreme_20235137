import shutil
from pathlib import Path

import pytest

from decision_system.dependencies import default_services
from decision_system.engine import DecisionEngine
from decision_system.store import Store
from decision_system.workflow import WorkflowRegistry

CONFIG_SRC = Path(__file__).resolve().parent.parent / "config" / "workflows"


@pytest.fixture
def config_dir(tmp_path):
    """A private copy of the workflow configs so tests can edit them."""
    dest = tmp_path / "workflows"
    shutil.copytree(CONFIG_SRC, dest)
    return dest


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def engine(config_dir, sleeps):
    return DecisionEngine(Store(), WorkflowRegistry(config_dir), default_services(), sleep=sleeps.append)


def loan(**overrides):
    data = {
        "applicant_id": "APP-GOOD",
        "full_name": "Asha Verma",
        "age": 32,
        "annual_income": 900000,
        "loan_amount": 800000,
        "employment_status": "employed",
    }
    data.update(overrides)
    return data


def claim(**overrides):
    data = {
        "claim_id": "CLM-1",
        "policy_number": "POL-1001",
        "claim_type": "medical",
        "claim_amount": 12000,
        "incident_date": "2025-06-01",
        "documents_attached": True,
    }
    data.update(overrides)
    return data
