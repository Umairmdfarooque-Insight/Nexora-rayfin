"""The business glossary (config/glossary.yaml): which words name a business term.

It replaces the hardcoded ALIASES of the old matcher. The file is read on every call, so an
edit takes effect without restarting the API. A malformed file is a ConfigError naming the
file and the line.
"""

from pathlib import Path

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "glossary.yaml"


def normalize(value: str) -> str:
    """Normalize names for comparison."""
    return "".join(char.lower() for char in value if char.isalnum())


def forms(value: str) -> set[str]:
    """A normalized name and its singular (a trailing "s" on a name longer than three letters)."""
    found = {value}
    if value.endswith("s") and len(value) > 3:
        found.add(value[:-1])
    return found


class Glossary:
    def __init__(self, terms: dict[str, list[str]]):
        # normalized term -> normalized words that name it (the term itself included)
        self.terms = {
            normalize(term): sorted({normalize(term), *(normalize(a) for a in words)} - {""})
            for term, words in terms.items()
        }

    def aliases_for(self, entity: str) -> list[str]:
        """The normalized words that name `entity`: the union over every term the entity (or
        its singular) is, or is an alias of. An unknown entity is named only by itself."""
        wanted = forms(normalize(entity))
        found: set[str] = set()
        for term, words in self.terms.items():
            if wanted & {w for word in words for w in forms(word)} or wanted & forms(term):
                found.update(words)
        return sorted(found) if found else sorted(w for w in wanted if w)


def load_glossary(path: Path | None = None) -> Glossary:
    explicit = path is not None
    path = path or CONFIG_PATH
    label = path.name
    from_fabric = not explicit and path is CONFIG_PATH and config_files.from_fabric()
    try:
        text = config_files.read_text(label) if from_fabric else path.read_text(encoding="utf-8")
        root = yaml.compose(text)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file {label} is missing ({path})") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        raise ConfigError(f"Configuration file {label} can't be read{where}: {getattr(exc, 'problem', exc)}") from None
    except (UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"Configuration file {label} can't be read: {exc}") from None
    except Exception as exc:  # the OneLake connector: a missing file, a network error, …
        if not from_fabric:
            raise
        raise ConfigError(f"Configuration file {label} is missing or can't be read from Files/config/: {exc}") from None

    def bad(node, reason: str) -> ConfigError:
        line = f" at line {node.start_mark.line + 1}" if node is not None else ""
        return ConfigError(f"Configuration file {label} is invalid{line}: {reason}")

    if not isinstance(root, yaml.MappingNode):
        raise bad(root, "expected a mapping with `terms`")
    terms_node = next((v for k, v in root.value if getattr(k, "value", None) == "terms"), None)
    if not isinstance(terms_node, yaml.MappingNode) or not terms_node.value:
        raise bad(terms_node or root, "`terms` must be a non-empty mapping of term to words")
    terms: dict[str, list[str]] = {}
    for key, value in terms_node.value:
        if not isinstance(key, yaml.ScalarNode) or not normalize(key.value):
            raise bad(key, "term names must be non-empty text")
        if not isinstance(value, yaml.SequenceNode) or not all(
            isinstance(item, yaml.ScalarNode) and normalize(item.value) for item in value.value
        ):
            raise bad(value, f"the words of {key.value!r} must be a list of text")
        terms[key.value] = [item.value for item in value.value]
    return Glossary(terms)
