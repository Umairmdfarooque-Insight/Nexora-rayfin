"""Gate G4 settings (T-504), from config/validation.yaml. Read on every call, so an edit takes
effect without restarting the API; a missing or invalid file is a CONFIG_ERROR."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "validation.yaml"


@dataclass(frozen=True)
class Tolerance:
    relative: float
    absolute: float

    def allows(self, value: float, reference: float) -> bool:
        return abs(value - reference) <= max(self.absolute, self.relative * abs(reference))


def load_tolerance(path: Path | None = None) -> Tolerance:
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
    section = data.get("reconciliation") if isinstance(data, dict) else None
    values = []
    for key in ("relative_tolerance", "absolute_tolerance"):
        value = section.get(key) if isinstance(section, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"Configuration file {label} is invalid: reconciliation.{key} must be a number of 0 or more")
        values.append(float(value))
    return Tolerance(relative=values[0], absolute=values[1])
