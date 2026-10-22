"""The approved conformed sources (T-304, business rule 5), from config/conformed_sources.yaml.
Read on every call, so an edit takes effect without restarting the API."""

from pathlib import Path

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "conformed_sources.yaml"


def load_conformed(path: Path | None = None) -> frozenset[str]:
    """Lower-cased "schema.table" names of the conformed sources."""
    explicit = path is not None
    path = path or CONFIG_PATH
    label = path.name
    from_fabric = not explicit and path is CONFIG_PATH and config_files.from_fabric()
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

    names = data.get("conformed") if isinstance(data, dict) else None
    if names is None and isinstance(data, dict) and "conformed" in data:
        names = []  # an empty list is written as `conformed:` in YAML
    if not isinstance(names, list) or not all(
        isinstance(n, str) and n.count(".") == 1 and all(n.split(".")) for n in names
    ):
        raise ConfigError(
            f"Configuration file {label} is invalid: expected `conformed`, a list of schema.table names"
        )
    return frozenset(n.strip().lower() for n in names)
