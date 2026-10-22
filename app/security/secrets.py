"""Secrets from Azure Key Vault (T-705, NFR-001).

With `KEY_VAULT_URL` set, the Azure OpenAI key and the API client secret are read from the vault
(`azure-openai-api-key`, `entra-api-client-secret`) and `.env` is for non-secret settings only. Without
it (local development) they come from the environment, as before.

**Fabric rework (T-705).** In a Fabric job the vault is reached with `notebookutils.credentials.getSecret`
(the job's own user, or the workspace identity if Key Vault doesn't grant the user — recorded once this
is confirmed against a live tenant, see fabric-hosting.md's platform-limits table); a User Data Function
must never call the model, so it never calls this module for `azure-openai-api-key` at all. Locally (and
in any other process, as a fallback) the vault is reached with `DefaultAzureCredential`: the managed
identity in Azure, the developer's own sign-in locally.

If the vault is configured but can't be reached, loading fails with a message that names `KEY_VAULT_URL`
(and never a secret). A secret that was rotated is re-read with `refresh_secret`.

This module imports nothing from `app.config` (or anything that imports it), so `app.config` can use it
while it loads; `app.runtime` is a leaf module with no such dependency, so importing it here is safe.
"""

import os
from typing import Any, Callable
from urllib.parse import urlparse

VAULT_ENV = "KEY_VAULT_URL"
_cache: dict[str, str] = {}
_credential: Any = None
_client_factory: Callable[[str], Any] | None = None  # tests put a fake vault here


class SecretsUnavailable(ValueError):
    """Key Vault is configured but a secret can't be had. The message never holds a secret."""


def vault_url() -> str | None:
    value = os.getenv(VAULT_ENV)
    return value.strip().rstrip("/") if value and value.strip() else None


def service_credential() -> Any:
    """The identity this service uses for service-to-service calls where Azure supports it: the managed
    identity when running in Azure, the developer's sign-in (Azure CLI, VS Code, environment) locally."""
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential

        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential


def _client(url: str) -> Any:
    if _client_factory is not None:
        return _client_factory(url)
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=url, credential=service_credential())


def _read_in_job(url: str, name: str) -> str:
    import notebookutils  # noqa: PLC0415  (only importable inside a Fabric notebook)

    return notebookutils.credentials.getSecret(url, name)


def read_vault_secret(name: str) -> str:
    """Read one secret from the vault. Raises SecretsUnavailable (naming KEY_VAULT_URL, or the secret's name)."""
    url = vault_url()
    if not url:
        raise SecretsUnavailable(f"{VAULT_ENV} is not set")
    host = urlparse(url).netloc or url
    from app.runtime import RUNTIME

    try:
        value = _read_in_job(url, name) if RUNTIME == "fabric_job" else _client(url).get_secret(name).value
    except Exception as exc:
        if type(exc).__name__ == "ResourceNotFoundError":
            raise SecretsUnavailable(f"The secret {name!r} is not in the Key Vault at {VAULT_ENV} ({host})") from None
        raise SecretsUnavailable(
            f"The Key Vault at {VAULT_ENV} ({host}) can't be reached or refused access ({type(exc).__name__}). "
            "Check the URL, the network and that the identity may read secrets."
        ) from None
    if not value:
        raise SecretsUnavailable(f"The secret {name!r} in the Key Vault at {VAULT_ENV} ({host}) is empty")
    return value


def load_secret(env_name: str, vault_name: str, required: bool = True) -> str | None:
    """The secret from Key Vault when `KEY_VAULT_URL` is set, else from the environment variable.

    A missing required environment value is the same ValueError as any other missing setting. In the
    vault, a missing optional secret is None; an unreachable vault is always SecretsUnavailable."""
    if vault_url():
        if vault_name in _cache:
            return _cache[vault_name]
        try:
            _cache[vault_name] = read_vault_secret(vault_name)
        except SecretsUnavailable as exc:
            if not required and "is not in the Key Vault" in str(exc):
                return None
            raise
        return _cache[vault_name]
    value = os.getenv(env_name)
    if value and value.strip():
        return value.strip()
    if required:
        raise ValueError(f"Missing required configuration: {env_name}. Check your .env file.")
    return None


def refresh_secret(vault_name: str) -> str | None:
    """Re-read a rotated secret from the vault (None if no vault is configured)."""
    if not vault_url():
        return None
    _cache.pop(vault_name, None)
    _cache[vault_name] = read_vault_secret(vault_name)
    return _cache[vault_name]


def reset_for_tests() -> None:
    global _credential, _client_factory
    _cache.clear()
    _credential = None
    _client_factory = None
