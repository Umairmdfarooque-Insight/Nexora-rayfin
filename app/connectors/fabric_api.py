"""Read-only client for the Fabric REST API (workspaces and Lakehouses)."""

import time
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.auth import FABRIC_SCOPE, get_token

FABRIC_API = "https://api.fabric.microsoft.com/v1"

MAX_RETRIES = 3  # retries after a 429, so up to 4 requests
MAX_RETRY_WAIT = 60  # seconds; caps a very large Retry-After
DEFAULT_RETRY_WAIT = 5
MAX_PAGES = 1000
TIMEOUT = 30


class FabricError(Exception):
    """Base class for Fabric API failures. Messages never contain tokens."""

    def __init__(self, message: str, request_id: str | None = None):
        self.request_id = request_id
        super().__init__(message)


class FabricForbidden(FabricError):
    """The identity may not see the resource."""


class FabricNotFound(FabricError):
    """The resource doesn't exist, or is not visible to the identity."""


class FabricRateLimited(FabricError):
    def __init__(self, message: str, retry_after: int, request_id: str | None = None):
        self.retry_after = retry_after
        super().__init__(message, request_id)


class FabricUpstreamError(FabricError):
    """Fabric returned a 5xx or an unusable response, or couldn't be reached."""


class FabricTimeout(FabricError):
    """A Fabric call timed out."""


def _request_id(response) -> str | None:
    headers = response.headers
    return headers.get("RequestId") or headers.get("x-ms-request-id")


def _retry_wait(response) -> int:
    try:
        wait = int(float(response.headers.get("Retry-After", DEFAULT_RETRY_WAIT)))
    except (TypeError, ValueError):
        wait = DEFAULT_RETRY_WAIT
    return min(max(wait, 1), MAX_RETRY_WAIT)


class FabricApiClient:
    def __init__(
        self,
        token_provider: Callable[[], str] | None = None,
        http_get: Callable[..., Any] = requests.get,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token = token_provider or (lambda: get_token(FABRIC_SCOPE))
        self._get = http_get
        self._sleep = sleep

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        retries = 0
        while True:
            try:
                response = self._get(
                    url,
                    headers={"Authorization": f"Bearer {self._token()}"},
                    params=params,
                    timeout=TIMEOUT,
                )
            except requests.Timeout:
                raise FabricTimeout("Fabric did not respond in time") from None
            except requests.RequestException as error:
                raise FabricUpstreamError(
                    f"Fabric could not be reached ({type(error).__name__})"
                ) from None

            status = response.status_code
            request_id = _request_id(response)

            if status == 429:
                wait = _retry_wait(response)
                if retries >= MAX_RETRIES:
                    raise FabricRateLimited(
                        "Fabric is throttling requests", wait, request_id
                    )
                retries += 1
                self._sleep(wait)
                continue
            if status in (401, 403):
                raise FabricForbidden("Fabric denied access", request_id)
            if status in (400, 404):
                raise FabricNotFound("Fabric resource not found", request_id)
            if status >= 400:
                raise FabricUpstreamError(
                    f"Fabric returned HTTP {status}", request_id
                )
            try:
                return response.json()
            except ValueError:
                raise FabricUpstreamError(
                    "Fabric returned an unreadable response", request_id
                ) from None

    def _get_all(self, url: str) -> list[dict]:
        """Follow continuationToken paging until every page is read."""
        items: list[dict] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            body = self._get_json(url, {"continuationToken": token} if token else None)
            items.extend(body.get("value", []))
            token = body.get("continuationToken")
            if not token:
                return items
            if token in seen:
                raise FabricUpstreamError("Fabric returned a repeating page token")
            seen.add(token)
        raise FabricUpstreamError("Fabric returned too many pages")

    def list_workspaces(self) -> list[dict]:
        return [
            {
                "id": w["id"],
                "display_name": w.get("displayName", ""),
                "type": w.get("type", ""),
            }
            for w in self._get_all(f"{FABRIC_API}/workspaces")
        ]

    def list_lakehouses(self, workspace_id: str) -> list[dict]:
        url = f"{FABRIC_API}/workspaces/{quote(workspace_id, safe='')}/lakehouses"
        return [self._lakehouse(workspace_id, item) for item in self._get_all(url)]

    def get_job_instance(self, workspace_id: str, item_id: str, job_instance_id: str) -> dict:
        """A `RunNotebook` job instance's `{status, failureReason, ...}` (T-102
        FR-7: whether a job ended without writing a result file)."""
        url = (
            f"{FABRIC_API}/workspaces/{quote(workspace_id, safe='')}"
            f"/items/{quote(item_id, safe='')}/jobs/instances/{quote(job_instance_id, safe='')}"
        )
        return self._get_json(url)

    @staticmethod
    def _lakehouse(workspace_id: str, item: dict) -> dict:
        name = item.get("displayName", "")
        endpoint = (item.get("properties") or {}).get("sqlEndpointProperties") or {}
        server = endpoint.get("connectionString")
        return {
            "id": item["id"],
            "display_name": name,
            "workspace_id": item.get("workspaceId", workspace_id),
            # The SQL endpoint's database carries the Lakehouse's name.
            "sql_endpoint": {"server": server, "database": name} if server else None,
        }
