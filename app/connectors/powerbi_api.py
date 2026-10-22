"""Power BI REST client for validating a deployed semantic model (T-504).

Refresh (Direct Lake framing) and DAX queries against a model in a workspace, as the signed-in
user. Calls are workspace-scoped (`/groups/{workspace}/datasets/{id}/...`), because a model in a
workspace other than "My workspace" isn't reachable without the group. A `429` is retried with the
`Retry-After` delay and then reported as an upstream error.
"""

import time
from typing import Any, Callable

import requests

from app.auth import POWERBI_SCOPE, get_token
from app.connectors.fabric_api import (
    MAX_RETRY_WAIT,
    FabricForbidden,
    FabricNotFound,
    FabricTimeout,
    FabricUpstreamError,
    _request_id,
    _retry_wait,
)

POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
MAX_RETRIES_429 = 5
DEFAULT_POLL_WAIT = 3
HTTP_TIMEOUT = 120
DONE = {"Completed"}
FAILED = {"Failed", "Disabled", "Cancelled"}


class RefreshFailed(FabricUpstreamError):
    """The refresh of the model ended in a failure; the message is Power BI's."""


class DaxError(Exception):
    """A DAX query was rejected or failed; the message is the engine's own."""


class ExportUnavailable(Exception):
    """A report can't be exported to an image here (no Premium or Fabric capacity, or the export
    was refused or failed). The message is Power BI's."""


def _json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _engine_message(body: dict) -> str:
    """The most specific text of an executeQueries error body."""
    error = body.get("error") or {}
    details = (error.get("pbi.error") or {}).get("details") or []
    texts = [str((d.get("detail") or {}).get("value", "")) for d in details if isinstance(d, dict)]
    text = " ".join(t for t in texts if t) or str(error.get("message") or error.get("code") or "The query failed")
    return text.strip()


class PowerBiClient:
    def __init__(
        self,
        token_provider: Callable[[], str] | None = None,
        http: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._token = token_provider or (lambda: get_token(POWERBI_SCOPE))
        self._http = http or (lambda *a, **kw: requests.request(*a, **kw))
        self._sleep = sleep
        self._clock = clock

    def _call(self, method: str, url: str, json: dict | None = None, ok_errors: tuple[int, ...] = ()):
        retries = 0
        while True:
            try:
                response = self._http(
                    method, url, headers={"Authorization": f"Bearer {self._token()}"}, json=json, timeout=HTTP_TIMEOUT
                )
            except requests.Timeout:
                raise FabricTimeout("Power BI did not respond in time") from None
            except requests.RequestException as error:
                raise FabricUpstreamError(f"Power BI could not be reached ({type(error).__name__})") from None
            status, request_id = response.status_code, _request_id(response)
            if status == 429:
                if retries >= MAX_RETRIES_429:
                    raise FabricUpstreamError("Power BI is throttling requests", request_id)
                retries += 1
                self._sleep(_retry_wait(response))
                continue
            if status in ok_errors:
                return response
            if status in (401, 403):
                raise FabricForbidden("Power BI denied access", request_id)
            if status == 404:
                raise FabricNotFound("Power BI dataset not found", request_id)
            if status >= 400:
                raise FabricUpstreamError(f"Power BI returned HTTP {status}", request_id)
            return response

    @staticmethod
    def _base(workspace_id: str, dataset_id: str) -> str:
        return f"{POWERBI_API}/groups/{workspace_id}/datasets/{dataset_id}"

    def refresh(self, workspace_id: str, dataset_id: str, timeout_s: int = 600) -> None:
        """Refresh (frame) the model and wait for it. Raises RefreshFailed or FabricTimeout."""
        base = self._base(workspace_id, dataset_id)
        started = self._call("POST", f"{base}/refreshes", {"type": "full"})
        request_id = started.headers.get("x-ms-request-id") or started.headers.get("RequestId")
        url = f"{base}/refreshes/{request_id}" if request_id else f"{base}/refreshes?$top=1"
        deadline = self._clock() + timeout_s
        while True:
            body = _json(self._call("GET", url))
            if not request_id:
                body = (body.get("value") or [{}])[0]
            status = body.get("status")
            if status in DONE:
                return
            if status in FAILED:
                detail = body.get("serviceExceptionJson") or body.get("extendedStatus") or status
                raise RefreshFailed(f"The refresh {str(status).lower()}: {detail}", _request_id(started))
            if self._clock() >= deadline:
                raise FabricTimeout("The refresh did not finish in time")
            self._sleep(DEFAULT_POLL_WAIT)

    def export_report_page(self, workspace_id: str, report_id: str, page_name: str, timeout_s: int = 300) -> bytes:
        """Export one page of a report to PNG and return the image. Raises ExportUnavailable if
        Power BI refuses or fails the export (it needs Fabric or Premium capacity) and FabricTimeout
        if it doesn't finish in time."""
        base = f"{POWERBI_API}/groups/{workspace_id}/reports/{report_id}"
        body = {"format": "PNG", "powerBIReportConfiguration": {"pages": [{"pageName": page_name}]}}
        started = self._call("POST", f"{base}/ExportTo", body, ok_errors=(400, 403, 404))
        if started.status_code in (400, 403, 404):
            raise ExportUnavailable(_engine_message(_json(started)) if started.status_code == 400 else f"HTTP {started.status_code}")
        export_id = _json(started).get("id")
        if not export_id:
            raise ExportUnavailable("Power BI accepted the export without an export id")
        deadline = self._clock() + timeout_s
        while True:
            state = _json(self._call("GET", f"{base}/exports/{export_id}"))
            status = state.get("status")
            if status == "Succeeded":
                break
            if status == "Failed":
                error = state.get("error") or {}
                raise ExportUnavailable(str(error.get("message") or error.get("code") or "the export failed"))
            if self._clock() >= deadline:
                raise FabricTimeout("The export did not finish in time")
            self._sleep(DEFAULT_POLL_WAIT)
        return self._call("GET", f"{base}/exports/{export_id}/file").content

    def execute_dax(self, workspace_id: str, dataset_id: str, dax: str, impersonate: str | None = None) -> list[dict]:
        """The rows of one DAX query. Raises DaxError with the engine's message if it fails."""
        body: dict = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        if impersonate:
            body["impersonatedUserName"] = impersonate
        response = self._call("POST", f"{self._base(workspace_id, dataset_id)}/executeQueries", body, ok_errors=(400,))
        data = _json(response)
        if response.status_code == 400:
            raise DaxError(_engine_message(data))
        result = (data.get("results") or [{}])[0]
        if result.get("error"):
            raise DaxError(_engine_message({"error": result["error"]}))
        tables = result.get("tables") or [{}]
        return list(tables[0].get("rows") or [])
