"""Workflow definitions loaded from JSON config, plus the request lifecycle."""
import json
from pathlib import Path

# --- lifecycle -----------------------------------------------------------

RECEIVED = "RECEIVED"
IN_PROGRESS = "IN_PROGRESS"
RETRY_PENDING = "RETRY_PENDING"
MANUAL_REVIEW = "MANUAL_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"

FINAL_STATES = {APPROVED, REJECTED}
TERMINAL_STAGE_STATUSES = {APPROVED, REJECTED, MANUAL_REVIEW}

ALLOWED_TRANSITIONS = {
    RECEIVED: {IN_PROGRESS},
    IN_PROGRESS: {IN_PROGRESS, RETRY_PENDING, MANUAL_REVIEW, APPROVED, REJECTED},
    RETRY_PENDING: {IN_PROGRESS},
    MANUAL_REVIEW: {APPROVED, REJECTED},
    APPROVED: set(),
    REJECTED: set(),
}


def can_transition(old, new):
    return new in ALLOWED_TRANSITIONS.get(old, set())


# --- definitions ---------------------------------------------------------

STAGE_TYPES = {"rules", "dependency", "terminal"}


class ConfigError(Exception):
    pass


class WorkflowDefinition:
    def __init__(self, raw):
        self.raw = raw
        self.name = raw.get("name")
        self.version = raw.get("version", 1)
        self.schema = raw.get("schema", {})
        self.start = raw.get("start")
        self.stages = raw.get("stages", {})
        self._check()

    def _check(self):
        if not self.name:
            raise ConfigError("workflow needs a name")
        if self.start not in self.stages:
            raise ConfigError(f"{self.name}: start stage '{self.start}' is not defined")

        def target(stage_name, key, value):
            if value not in self.stages:
                raise ConfigError(f"{self.name}.{stage_name}: {key} -> unknown stage '{value}'")

        for sname, stage in self.stages.items():
            stype = stage.get("type")
            if stype not in STAGE_TYPES:
                raise ConfigError(f"{self.name}.{sname}: unknown stage type '{stype}'")

            if stype == "rules":
                seen = set()
                for rule in stage.get("rules", []):
                    for key in ("id", "when", "goto"):
                        if key not in rule:
                            raise ConfigError(f"{self.name}.{sname}: rule missing '{key}'")
                    if rule["id"] in seen:
                        raise ConfigError(f"{self.name}.{sname}: duplicate rule id '{rule['id']}'")
                    seen.add(rule["id"])
                    target(sname, f"rule {rule['id']}", rule["goto"])
                if "default" not in stage:
                    raise ConfigError(f"{self.name}.{sname}: rules stage needs a 'default' target")
                target(sname, "default", stage["default"])

            elif stype == "dependency":
                for key in ("service", "on_success", "on_failure"):
                    if key not in stage:
                        raise ConfigError(f"{self.name}.{sname}: missing '{key}'")
                target(sname, "on_success", stage["on_success"])
                target(sname, "on_failure", stage["on_failure"])
                if stage.get("on_exhausted", "on_failure") not in ("retry_later", "on_failure"):
                    raise ConfigError(f"{self.name}.{sname}: on_exhausted must be 'retry_later' or 'on_failure'")

            elif stype == "terminal":
                if stage.get("status") not in TERMINAL_STAGE_STATUSES:
                    raise ConfigError(f"{self.name}.{sname}: bad terminal status {stage.get('status')}")


class WorkflowRegistry:
    """Loads every *.json in a directory. reload() picks up edited configs."""

    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)
        self._defs = {}
        self.reload()

    def reload(self):
        loaded = {}
        for path in sorted(self.config_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                wf = WorkflowDefinition(raw)
            except (json.JSONDecodeError, ConfigError) as exc:
                raise ConfigError(f"{path.name}: {exc}") from exc
            loaded[wf.name] = wf
        # only swap once everything parsed, so a bad edit can't half-apply
        self._defs = loaded
        return {name: wf.version for name, wf in loaded.items()}

    def get(self, name):
        if name not in self._defs:
            raise ConfigError(f"unknown workflow '{name}'")
        return self._defs[name]

    def names(self):
        return sorted(self._defs)
