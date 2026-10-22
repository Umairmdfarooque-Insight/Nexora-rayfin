"""Approved derivation rules (T-403, business rule 4), from config/derivation_rules.yaml.

A rule says how a mapping between two entities may be derived (or how duplicate pairs in a
mapping table may be removed) without guessing. Read on every call.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT
from app.discovery.glossary import normalize

CONFIG_PATH = PROJECT_ROOT / "config" / "derivation_rules.yaml"


@dataclass(frozen=True)
class DerivationRule:
    id: str
    entities: tuple[str, str]
    description: str
    approved_by: str
    deduplicate: bool = False

    def covers(self, a: str, b: str) -> bool:
        return {normalize(e) for e in self.entities} == {normalize(a), normalize(b)}


def load_rules(path: Path | None = None) -> list[DerivationRule]:
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

    def bad(reason: str) -> ConfigError:
        return ConfigError(f"Configuration file {label} is invalid: {reason}")

    items = data.get("rules") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict) and "rules" in data:
        items = []  # an empty list is written as `rules:` in YAML
    if not isinstance(items, list):
        raise bad("expected `rules`, a list")
    rules: list[DerivationRule] = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise bad(f"rule {i} must be a mapping")
        entities = item.get("entities")
        if not (isinstance(entities, list) and len(entities) == 2 and all(isinstance(e, str) and normalize(e) for e in entities)):
            raise bad(f"rule {i}: `entities` must list exactly two entity names")
        for key in ("id", "description", "approved_by"):
            if not (isinstance(item.get(key), str) and item[key].strip()):
                raise bad(f"rule {i}: `{key}` must be non-empty text")
        if not isinstance(item.get("deduplicate", False), bool):
            raise bad(f"rule {i}: `deduplicate` must be true or false")
        rules.append(
            DerivationRule(
                id=item["id"].strip(),
                entities=(entities[0].strip(), entities[1].strip()),
                description=item["description"].strip(),
                approved_by=item["approved_by"].strip(),
                deduplicate=item.get("deduplicate", False),
            )
        )
    if len({r.id for r in rules}) != len(rules):
        raise bad("rule ids must be unique")
    return rules
