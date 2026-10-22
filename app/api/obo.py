"""On-behalf-of (OBO) tokens: call Fabric as the signed-in user (T-105, FR-004).

The API exchanges the user's access token for a Fabric or OneLake token, so the
workspace and Lakehouse lists reflect what that user can access.
"""

import logging
from functools import lru_cache
from typing import Any, Callable

import msal

from app.api.security import LOGIN_HOST, AuthConfigError
from app.config import ENTRA_API_CLIENT_ID, ENTRA_API_CLIENT_SECRET, ENTRA_TENANT_ID

logger = logging.getLogger(__name__)

# Errors that mean the user (or an admin) still has to consent.
CONSENT_ERRORS = {"interaction_required", "consent_required", "invalid_grant"}


class ConsentRequired(Exception):
    """The user hasn't consented to the API calling Fabric on their behalf."""


class OboFailed(Exception):
    """The token exchange failed for a reason other than consent."""


@lru_cache(maxsize=1)
def _msal_app() -> Any:
    missing = [
        name
        for name, value in (
            ("ENTRA_TENANT_ID", ENTRA_TENANT_ID),
            ("ENTRA_API_CLIENT_ID", ENTRA_API_CLIENT_ID),
            ("ENTRA_API_CLIENT_SECRET", ENTRA_API_CLIENT_SECRET),
        )
        if not value
    ]
    if missing:
        raise AuthConfigError(
            f"Calling Fabric as the signed-in user is not configured. "
            f"Set {', '.join(missing)} in .env."
        )
    # The app keeps an in-memory token cache, so repeated exchanges for the
    # same user token are served from it.
    return msal.ConfidentialClientApplication(
        ENTRA_API_CLIENT_ID,
        client_credential=ENTRA_API_CLIENT_SECRET,
        authority=f"{LOGIN_HOST}/{ENTRA_TENANT_ID}",
    )


def get_msal_app() -> Any:
    return _msal_app()


def obo_token_provider(
    msal_app: Any, user_token: str, scope: str
) -> Callable[[], str]:
    """A function that returns a token for `scope` on behalf of `user_token`."""

    def provide() -> str:
        result = msal_app.acquire_token_on_behalf_of(
            user_assertion=user_token, scopes=[scope]
        )
        if "access_token" in result:
            return result["access_token"]
        error = result.get("error", "unknown_error")
        # Log the error code only: descriptions can carry tokens and claims.
        logger.warning("OBO token exchange failed: %s", error)
        if error in CONSENT_ERRORS:
            raise ConsentRequired(
                "Consent is required before Rayfin can read Fabric as you. "
                "Ask your administrator to grant consent to the Nexora API, "
                "or sign out and sign in again to approve it."
            )
        raise OboFailed("Could not get a Fabric token on your behalf")

    return provide
