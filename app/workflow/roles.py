"""Role lookup for approvals (CONTRACTS.md#roles). Roles come from config/roles.yaml, which is
read on every call so an edit takes effect without restarting the API."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from app import config_files
from app.compiler.critical_terms import ConfigError
from app.config import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "roles.yaml"
KNOWN_ROLES = (
    "business_owner",
    "bi_engineer",
    "security_reviewer",
    "project_reviewer",
    "data_engineer",
    "release_approver",
)


@dataclass(frozen=True)
class Roles:
    members: dict[str, frozenset[str]]  # role -> lower-cased UPNs

    def roles_of(self, upn: str) -> list[str]:
        key = (upn or "").strip().lower()
        return [role for role in KNOWN_ROLES if key and key in self.members.get(role, frozenset())]

    def has(self, upn: str, role: str) -> bool:
        return role in self.roles_of(upn)


def load_roles(path: Path | None = None) -> Roles:
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

    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, dict):
        raise bad("expected a mapping `roles` of role name to a list of UPNs")
    members: dict[str, frozenset[str]] = {}
    for role, upns in roles.items():
        if role not in KNOWN_ROLES:
            raise bad(f"unknown role {role!r}")
        if upns is None:
            upns = []
        if not isinstance(upns, list) or not all(isinstance(u, str) and u.strip() for u in upns):
            raise bad(f"{role} must be a list of UPNs")
        members[role] = frozenset(u.strip().lower() for u in upns)
    return Roles(members=members)
