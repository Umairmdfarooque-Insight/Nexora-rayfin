import os
from pathlib import Path

from dotenv import load_dotenv

from app.runtime import RUNTIME

PROJECT_ROOT = Path(__file__).resolve().parents[1]

VARIABLE_LIBRARY_NAME = "nexora_settings"

# Settings resolve in this order, first non-blank value wins: the process
# environment, the Variable library nexora_settings (Fabric only), then .env
# (local only). .env is loaded once, from the project root, so the result
# doesn't depend on the working directory. A value already in the process
# environment is never overridden by .env.
if RUNTIME == "local":
    _environ_before_dotenv = {k for k, v in os.environ.items() if v and v.strip()}
    load_dotenv(PROJECT_ROOT / ".env", override=False)
else:
    _environ_before_dotenv = set()

from app.security.secrets import SecretsUnavailable, load_secret  # noqa: E402  (after .env is loaded)

_library = None
_library_tried = False


def _variable_library():
    """The nexora_settings Variable library object, fetched once. `None` outside
    Fabric, or when it can't be read (callers then treat the setting as unset)."""
    global _library, _library_tried

    if _library_tried:
        return _library

    _library_tried = True

    if RUNTIME == "local":
        return None

    try:
        import notebookutils  # noqa: PLC0415  (only importable in Fabric)
    except ImportError:
        return None

    try:
        _library = notebookutils.variableLibrary.getLibrary(VARIABLE_LIBRARY_NAME)
    except Exception:
        _library = None

    return _library


def _library_value(name: str) -> str | None:
    library = _variable_library()

    if library is None:
        return None

    try:
        value = library.getVariable(name)
    except Exception:
        return None

    if value is None:
        return None

    text = str(value)
    return text if text.strip() else None


def get_required_setting(name: str) -> str:
    value = get_optional_setting(name)

    if value is None:
        raise ValueError(
            f"Missing required configuration: {name}. "
            "Check the nexora_settings variable library or your .env file."
        )

    return value


def get_optional_setting(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)

    if value and value.strip():
        return value.strip()

    if RUNTIME != "local":
        value = _library_value(name)
        if value:
            return value

    return default


def setting_source(name: str) -> str | None:
    """Where `name`'s current value came from: "environment", "nexora_settings" or
    ".env". `None` if it doesn't resolve. Never returns the value itself."""
    if RUNTIME == "local":
        if name in _environ_before_dotenv:
            return "environment"
        value = os.environ.get(name)
        return ".env" if value and value.strip() else None

    value = os.environ.get(name)
    if value and value.strip():
        return "environment"

    return "nexora_settings" if _library_value(name) else None


def required_setting_names() -> list[str]:
    """Settings every runtime needs for the model call to work. Not enforced here:
    `app.llm.client` requires them at its own import, so a User Data Function that
    never imports it (UDFs make no model call) is unaffected. Used by the settings
    smoke check (T-003)."""
    return ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_KEY"]


APP_NAME = get_optional_setting("APP_NAME", "Nexora")
UI_APP_NAME = get_optional_setting("UI_APP_NAME", "Rayfin")
ENVIRONMENT = get_optional_setting("ENVIRONMENT", "development")

# UDFs make no model call (fabric-hosting.md#operation-hosting-rule). These three are
# therefore left optional here in every runtime; app.llm.client requires them again,
# at its own import, so importing it (never done by a UDF handler) is what enforces it.
AZURE_OPENAI_ENDPOINT = (get_optional_setting("AZURE_OPENAI_ENDPOINT") or "").rstrip("/") or None
AZURE_OPENAI_DEPLOYMENT = get_optional_setting("AZURE_OPENAI_DEPLOYMENT")


def _dev_variable_library_secret(env_name: str) -> str | None:
    """Interim rule until T-705: in a development job, a secret may come from the
    Variable library when Key Vault and the environment don't have it. Secrets are
    never read from the Variable library outside this one case."""
    if RUNTIME == "fabric_job" and ENVIRONMENT == "development":
        return _library_value(env_name)
    return None


# Secrets come from Azure Key Vault when KEY_VAULT_URL is set (T-705), from the
# environment or (in a development job, see above) the Variable library otherwise.
# An unreachable vault stops the process here, with a message that names KEY_VAULT_URL.
try:
    AZURE_OPENAI_API_KEY = load_secret(
        "AZURE_OPENAI_API_KEY", "azure-openai-api-key", required=False
    ) or _dev_variable_library_secret("AZURE_OPENAI_API_KEY")
    ENTRA_API_CLIENT_SECRET = load_secret("ENTRA_API_CLIENT_SECRET", "entra-api-client-secret", required=False)
except SecretsUnavailable as _error:
    raise ValueError(f"CONFIG_ERROR: {_error}") from None

FABRIC_WORKSPACE_ID = get_optional_setting("FABRIC_WORKSPACE_ID")
FABRIC_LAKEHOUSE_ID = get_optional_setting("FABRIC_LAKEHOUSE_ID")
FABRIC_WORKSPACE_NAME = get_optional_setting("FABRIC_WORKSPACE_NAME")
FABRIC_LAKEHOUSE_NAME = get_optional_setting("FABRIC_LAKEHOUSE_NAME")
FABRIC_SQL_SERVER = get_optional_setting("FABRIC_SQL_SERVER")
FABRIC_SQL_DATABASE = get_optional_setting("FABRIC_SQL_DATABASE")

# RUN_STORE=fabric_sql (T-101): the control database that holds run manifests.
RUN_STORE = get_optional_setting("RUN_STORE", "file")
FABRIC_CONTROL_DB_SERVER = get_optional_setting("FABRIC_CONTROL_DB_SERVER")
FABRIC_CONTROL_DB_NAME = get_optional_setting("FABRIC_CONTROL_DB_NAME")

# The Nexora home workspace's own items (T-005). Blank until deploy_home writes them
# into the Variable library, or they are set locally for development.
NEXORA_HOME_WORKSPACE_ID = get_optional_setting("NEXORA_HOME_WORKSPACE_ID")
NEXORA_CONTROL_LAKEHOUSE_ID = get_optional_setting("NEXORA_CONTROL_LAKEHOUSE_ID")
NEXORA_STAGE_RUNNER_ID = get_optional_setting("NEXORA_STAGE_RUNNER_ID")

# Entra ID sign-in for the API (T-105). Optional at import so scripts and the
# CLI still run; the API refuses every protected request until they are set.
ENTRA_TENANT_ID = get_optional_setting("ENTRA_TENANT_ID")
ENTRA_API_CLIENT_ID = get_optional_setting("ENTRA_API_CLIENT_ID")
