import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

from app.runtime import RUNTIME

STORAGE_SCOPE = "https://storage.azure.com/.default"
SQL_SCOPE = "https://database.windows.net/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
# The generic Kusto/Eventhouse resource (T-704's telemetry sink); not yet confirmed against a live
# tenant whether this is the right audience for an Eventstream custom endpoint, or whether a
# cluster-specific resource is needed instead -- see fabric-hosting.md's platform-limits table.
KUSTO_SCOPE = "https://kusto.kusto.windows.net/.default"

CACHE_NAME = "nexora"

# notebookutils.credentials.getToken() takes an "audience": one of its four documented
# short keys (storage, pbi, keyvault, kusto - the pbi key also covers the Fabric REST
# API), or a raw resource/".default" string for anything else. There is no short key
# for the SQL endpoint audience; the raw resource string is what community samples use
# against it and is what we fall back to. See fabric-hosting.md's platform-limits table.
_JOB_AUDIENCE_BY_SCOPE = {
    STORAGE_SCOPE: "storage",
    POWERBI_SCOPE: "pbi",
    FABRIC_SCOPE: "pbi",
    KUSTO_SCOPE: "kusto",
}
_JOB_TOKEN_SAFETY_WINDOW_SECONDS = 300

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_credential: TokenCredential | None = None
_cache_warned = False
_record_saved = False

_job_token_cache: dict[str, tuple[str, float]] = {}
_job_token_lock = threading.Lock()


class ConsentRequired(RuntimeError):
    """A job couldn't get a user-delegated token for a scope (no consent, or the
    audience isn't supported for this identity). Never carries the token."""


# --- local development: one process-wide interactive credential --------------------


def _is_cache_failure(error: Exception) -> bool:
    """True when the error comes from the encrypted token cache, not from sign-in."""
    if isinstance(error, ClientAuthenticationError):
        return False

    module = type(error).__module__ or ""
    return module.startswith("msal_extensions") or isinstance(
        error, (ImportError, OSError, ValueError)
    )


def _record_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / ".IdentityService" / f"{CACHE_NAME}_auth_record.json"


def _load_record() -> AuthenticationRecord | None:
    """Load the saved account identifiers (no tokens) used to read the cache silently."""
    try:
        return AuthenticationRecord.deserialize(
            _record_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError, KeyError):
        return None


def _save_record(record: AuthenticationRecord) -> None:
    try:
        path = _record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record.serialize(), encoding="utf-8")
    except OSError as error:
        logger.warning("Could not save the sign-in record (%s).", type(error).__name__)


def _build_credential(persistent: bool) -> TokenCredential:
    if persistent:
        record = _load_record()

        return InteractiveBrowserCredential(
            cache_persistence_options=TokenCachePersistenceOptions(name=CACHE_NAME),
            authentication_record=record,
        )

    return InteractiveBrowserCredential()


def get_credential() -> TokenCredential:
    """Return the one process-wide local credential, creating it on first use.

    Local development only; Fabric runtimes never create a credential (a job uses
    notebookutils.credentials, and a UDF gets no source tokens at all)."""
    global _credential

    if RUNTIME != "local":
        raise RuntimeError(
            "get_credential() is local-only; a job uses notebookutils.credentials "
            "and a User Data Function has no source tokens."
        )

    with _lock:
        if _credential is None:
            _credential = _build_credential(persistent=True)

        return _credential


def _fall_back_to_memory_cache(error: Exception) -> TokenCredential:
    global _credential, _cache_warned, _record_saved

    with _lock:
        if not _cache_warned:
            logger.warning(
                "Encrypted token cache is unavailable (%s); "
                "using an in-memory cache for this process.",
                type(error).__name__,
            )
            _cache_warned = True

        _credential = _build_credential(persistent=False)
        _record_saved = True

        return _credential


def _request_token(credential: TokenCredential, scope: str) -> str:
    global _record_saved

    try:
        # Without a saved record the credential can't read the cache silently, so
        # the first sign-in captures one (account identifiers only, no tokens).
        if isinstance(credential, InteractiveBrowserCredential) and not _record_saved:
            if _load_record() is None:
                _save_record(credential.authenticate(scopes=[scope]))

            _record_saved = True

        return credential.get_token(scope).token
    except ClientAuthenticationError:
        raise ClientAuthenticationError(
            "Azure sign-in failed or was cancelled. "
            "Run the command again and complete the browser sign-in."
        ) from None


def _get_token_local(scope: str) -> str:
    try:
        return _request_token(get_credential(), scope)
    except Exception as error:
        if not _is_cache_failure(error):
            raise

        return _request_token(_fall_back_to_memory_cache(error), scope)


# --- Fabric job: the user the job runs as, through notebookutils -------------------


def _decode_jwt_exp(token: str) -> float | None:
    """The token's `exp` claim, read without verifying the signature (Fabric issued
    it); used only to decide when our own cache should refresh it."""
    try:
        payload_segment = token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        return float(payload["exp"])
    except Exception:
        return None


def _get_token_job(scope: str) -> str:
    with _job_token_lock:
        cached = _job_token_cache.get(scope)
        if cached and cached[1] - _JOB_TOKEN_SAFETY_WINDOW_SECONDS > time.time():
            return cached[0]

    audience = _JOB_AUDIENCE_BY_SCOPE.get(scope, scope)

    try:
        import notebookutils  # noqa: PLC0415  (only importable in a Fabric notebook)

        token = notebookutils.credentials.getToken(audience)
    except Exception as error:
        raise ConsentRequired(
            f"Could not get a token for {scope} ({type(error).__name__})"
        ) from error

    expiry = _decode_jwt_exp(token)
    if expiry is None:
        expiry = time.time() + 3600

    with _job_token_lock:
        _job_token_cache[scope] = (token, expiry)

    return token


def current_identity() -> dict | None:
    """`{oid, upn, name}` of the user a job runs as; `None` locally, and `None` if
    the runtime context doesn't carry it. A User Data Function reads the caller from
    its own invocation context instead (see CONTRACTS.md#fabric-operations)."""
    if RUNTIME != "fabric_job":
        return None

    try:
        import notebookutils  # noqa: PLC0415

        context = notebookutils.runtime.context
        user_id = context.get("userId")
        user_name = context.get("userName")
    except Exception:
        return None

    if not user_id and not user_name:
        return None

    # notebookutils.runtime.context documents userId/userName, not a separate upn; the
    # same name is used for both until this is confirmed against a live tenant (T-105).
    return {"oid": user_id, "upn": user_name, "name": user_name}


# --- Fabric User Data Function: no source tokens at all -----------------------------


def _get_token_udf(scope: str) -> str:
    raise RuntimeError(
        "Source access is not allowed in a User Data Function; run this operation as a job"
    )


# --- single entry point --------------------------------------------------------------


def get_token(scope: str) -> str:
    """Return an access token for `scope`, refreshing it silently when needed.

    Which identity and cache this uses depends on `app.runtime.RUNTIME`: the shared
    interactive credential locally, the job's own user through notebookutils in a
    Fabric job, and never (a RuntimeError) in a User Data Function."""
    if RUNTIME == "fabric_udf":
        return _get_token_udf(scope)
    if RUNTIME == "fabric_job":
        return _get_token_job(scope)
    return _get_token_local(scope)
