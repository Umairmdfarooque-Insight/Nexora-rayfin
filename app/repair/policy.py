"""The repair policy (T-505): config/repair_policy.yaml and how a failed check becomes a repair code.

The classification of an error is policy, never a model decision. Read on every call, so an edit
takes effect without restarting the API; a missing or invalid file is a CONFIG_ERROR.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT
from app.contracts.validation_report import ValidationCheck

CONFIG_PATH = PROJECT_ROOT / "config" / "repair_policy.yaml"
CLASSES = ("deterministic", "llm", "user_input")


@dataclass(frozen=True)
class RepairPolicy:
    max_attempts: int
    policy: dict[str, str]
    default: str

    def classify(self, repair_code: str) -> str:
        return self.policy.get(repair_code, self.default)


def load_policy(path: Path | None = None) -> RepairPolicy:
    path = path or CONFIG_PATH
    label = path.name
    from_fabric = path is CONFIG_PATH and config_files.from_fabric()
    try:
        text = config_files.read_text(label) if from_fabric else path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file {label} is missing ({path})") from None
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"Configuration file {label} can't be read: {exc}") from None
    except Exception as exc:  # the OneLake connector: a missing file, a network error, …
        if not from_fabric:
            raise
        raise ConfigError(f"Configuration file {label} is missing or can't be read from Files/config/: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration file {label} is invalid: expected a mapping")
    attempts = data.get("max_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ConfigError(f"Configuration file {label} is invalid: max_attempts must be a whole number of 1 or more")
    policy = data.get("policy")
    if not isinstance(policy, dict) or not all(isinstance(k, str) for k in policy):
        raise ConfigError(f"Configuration file {label} is invalid: policy must map repair codes to a class")
    default = data.get("default", "user_input")
    for name, value in [*policy.items(), ("default", default)]:
        if value not in CLASSES:
            raise ConfigError(f"Configuration file {label} is invalid: {name} must be one of {', '.join(CLASSES)}")
    return RepairPolicy(max_attempts=attempts, policy=dict(policy), default=default)


FORMAT_STRING = re.compile(r"format\s*string", re.IGNORECASE)
COLUMN_MISSING = re.compile(r"column\s+['\"\[]?([\w .]+?)['\"\]]?\s+(?:was\s+)?(?:not found|does not exist|doesn't exist|is missing|missing)", re.IGNORECASE)
NAMED = re.compile(r"'([^']+)'|\[([^\]]+)\]")


def repair_code(check: ValidationCheck) -> str:
    """The policy key of a failed Gate G4 check."""
    code, message = check.code or "", check.message
    if FORMAT_STRING.search(message) and code in ("DEPLOY_FAILED", "REFRESH_FAILED", "DAX_ERROR"):
        return "FORMAT_STRING_INVALID"
    if code in ("REFRESH_FAILED", "DEPLOY_FAILED") and COLUMN_MISSING.search(message):
        return "SOURCE_COLUMN_MISSING"
    if code == "UNKNOWN_REFERENCE":
        return "DAX_REFERENCE"
    if code == "DAX_ERROR":
        return "DAX_REFERENCE" if re.search(r"not found|cannot find|can't find|unknown|does not exist", message, re.IGNORECASE) else "DAX_SYNTAX"
    if code == "RELATIONSHIP_INVALID":
        return "REL_DUPLICATE_KEYS"
    return code or check.id


def named_objects(message: str) -> list[str]:
    """Every name in quotes or brackets in a message, in order."""
    return [a or b for a, b in NAMED.findall(message)]


def missing_column(message: str) -> str | None:
    m = COLUMN_MISSING.search(message)
    return m.group(1).strip() if m else None
