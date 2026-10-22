"""Entra ID bearer-token validation for the API (T-105, NFR-001, FR-018).

The token is validated deterministically: signature (tenant JWKS, cached),
issuer, audience, expiry, tenant and the `access_as_user` scope. There is no
bypass: if the settings are missing, protected endpoints fail with CONFIG_ERROR.
Tokens are never logged.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import ENTRA_API_CLIENT_ID, ENTRA_TENANT_ID

REQUIRED_SCOPE = "access_as_user"
LOGIN_HOST = "https://login.microsoftonline.com"
CLOCK_SKEW_SECONDS = 60


class AuthError(Exception):
    """The caller isn't authenticated. `code` is UNAUTHENTICATED or TOKEN_EXPIRED."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class AuthConfigError(Exception):
    """Entra settings are missing, so no request can be validated."""


class AuthUpstreamError(Exception):
    """The tenant's signing keys couldn't be fetched."""


class Principal(BaseModel):
    oid: str
    upn: str
    name: str


@dataclass(frozen=True)
class AuthContext:
    principal: Principal
    token: str  # kept only to call Fabric on the user's behalf; never logged


class TokenValidator:
    def __init__(self, tenant_id: str, api_client_id: str, jwks_client: Any = None):
        self.tenant_id = tenant_id.lower()
        self.api_client_id = api_client_id
        # Tokens may be v2 (login.microsoftonline.com) or v1 (sts.windows.net),
        # depending on the API registration's accessTokenAcceptedVersion.
        self.issuers = [
            f"{LOGIN_HOST}/{tenant_id}/v2.0",
            f"https://sts.windows.net/{tenant_id}/",
        ]
        self.audiences = [api_client_id, f"api://{api_client_id}"]
        # PyJWKClient caches the key set, so keys are not fetched on every call.
        self.jwks_client = jwks_client or jwt.PyJWKClient(
            f"{LOGIN_HOST}/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=3600,
        )

    def validate(self, token: str) -> Principal:
        try:
            key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=self.issuers,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("TOKEN_EXPIRED", "The access token has expired") from None
        except jwt.PyJWKClientConnectionError:
            raise AuthUpstreamError("Could not fetch the tenant signing keys") from None
        except jwt.PyJWTError:
            # Bad signature, wrong audience or issuer, malformed token, unknown key.
            raise AuthError("UNAUTHENTICATED", "The access token is not valid") from None

        if str(claims.get("tid", "")).lower() != self.tenant_id:
            raise AuthError("UNAUTHENTICATED", "The access token is for another tenant")
        if REQUIRED_SCOPE not in str(claims.get("scp", "")).split():
            raise AuthError(
                "UNAUTHENTICATED", f"The access token lacks the {REQUIRED_SCOPE} scope"
            )

        upn = (
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("unique_name")
        )
        oid = claims.get("oid")
        if not oid or not upn:
            raise AuthError("UNAUTHENTICATED", "The access token has no signed-in user")
        return Principal(oid=oid, upn=upn, name=claims.get("name") or upn)


@lru_cache(maxsize=1)
def _configured_validator() -> TokenValidator:
    missing = [
        name
        for name, value in (
            ("ENTRA_TENANT_ID", ENTRA_TENANT_ID),
            ("ENTRA_API_CLIENT_ID", ENTRA_API_CLIENT_ID),
        )
        if not value
    ]
    if missing:
        raise AuthConfigError(
            f"Authentication is not configured. Set {', '.join(missing)} in .env."
        )
    return TokenValidator(ENTRA_TENANT_ID, ENTRA_API_CLIENT_ID)


def get_validator() -> TokenValidator:
    return _configured_validator()


# Declares the bearer scheme in OpenAPI, so /docs shows an Authorize button.
# auto_error is off so a missing token gets the shared error envelope.
_bearer = HTTPBearer(auto_error=False, description="Entra access token for the Nexora API")


def authenticate(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    validator: TokenValidator = Depends(get_validator),
) -> AuthContext:
    """Validate the request's bearer token. Runs once per request."""
    if credentials is None or not credentials.credentials.strip():
        raise AuthError("UNAUTHENTICATED", "A bearer token is required")
    token = credentials.credentials.strip()
    return AuthContext(principal=validator.validate(token), token=token)


def current_user(context: AuthContext = Depends(authenticate)) -> Principal:
    return context.principal


def user_token(context: AuthContext = Depends(authenticate)) -> str:
    return context.token
