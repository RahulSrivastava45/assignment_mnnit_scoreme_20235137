"""Wiring: build an engine from a config directory and a database path."""
from pathlib import Path

from .dependencies import default_services
from .engine import DecisionEngine
from .store import Store
from .workflow import WorkflowRegistry

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "workflows"
DEFAULT_DB = ROOT / "data" / "decisions.db"


def build_engine(config_dir=DEFAULT_CONFIG, db_path=DEFAULT_DB, services=None):
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return DecisionEngine(Store(str(db_path)), WorkflowRegistry(config_dir), services or default_services())
