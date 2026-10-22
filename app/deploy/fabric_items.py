"""Create and update Fabric items (semantic models) in the sandbox workspace.

Fabric's create and update calls can be long-running operations: the answer is
`202` with a `Location` header that is polled until it reports `Succeeded` or
`Failed`. Only the configured sandbox workspace is a valid target; promotion
(T-702) uses its own gated path and passes `allow_any_workspace=True`.
"""

import base64
import hashlib
import time
from pathlib import Path
from typing import Any, Callable

import requests

from app.auth import FABRIC_SCOPE, get_token
from app.compiler.critical_terms import ConfigError
from app.config import get_optional_setting
from app.connectors.fabric_api import (
    FABRIC_API,
    MAX_RETRY_WAIT,
    FabricForbidden,
    FabricNotFound,
    FabricRateLimited,
    FabricTimeout,
    FabricUpstreamError,
    _request_id,
    _retry_wait,
)

MAX_RETRIES_429 = 5
DEFAULT_POLL_WAIT = 2
HTTP_TIMEOUT = 60
MAX_PAGES = 1000

SEMANTIC_MODEL = "SemanticModel"
NOTEBOOK = "Notebook"
REPORT = "Report"


class TargetNotAllowed(Exception):
    """The workspace is not the configured sandbox workspace."""


class NameConflict(Exception):
    """An item with this name exists in the workspace but has another type."""

    def __init__(self, name: str, item_type: str):
        self.item_type = item_type
        super().__init__(f"The name {name!r} is used by a {item_type} item")


class InvalidDefinition(ValueError):
    """The TMDL folder is missing required files."""


class FabricOperationFailed(FabricUpstreamError):
    """Fabric rejected a request or a long-running operation failed.

    `details` holds Fabric's own error (code, message), unchanged."""

    def __init__(self, message: str, request_id: str | None = None, details: dict | None = None):
        self.details = details or {}
        super().__init__(message, request_id)


class NotebookRunFailed(FabricOperationFailed):
    """A notebook run ended without success; `run_id` identifies it in Fabric's monitoring."""

    def __init__(self, message: str, request_id: str | None, details: dict, run_id: str):
        self.run_id = run_id
        super().__init__(message, request_id, details)


def get_sandbox_workspace_id() -> str | None:
    return get_optional_setting("SANDBOX_WORKSPACE_ID") or get_optional_setting(
        "FABRIC_SANDBOX_WORKSPACE_ID"
    )


def tmdl_parts(tmdl_dir: Path) -> list[dict]:
    """The definition parts of a TMDL folder: definition.pbism and definition/**."""
    tmdl_dir = Path(tmdl_dir)
    pbism = tmdl_dir / "definition.pbism"
    if not pbism.is_file():
        raise InvalidDefinition(f"definition.pbism not found in {tmdl_dir}")
    if not (tmdl_dir / "definition").is_dir():
        raise InvalidDefinition(f"definition folder not found in {tmdl_dir}")
    files = [pbism] + sorted(p for p in (tmdl_dir / "definition").rglob("*") if p.is_file())
    return [
        {
            "path": f.relative_to(tmdl_dir).as_posix(),
            "payload": base64.b64encode(f.read_bytes()).decode("ascii"),
            "payloadType": "InlineBase64",
        }
        for f in files
    ]


def pbir_parts(pbir_dir: Path) -> list[dict]:
    """The definition parts of a PBIR folder: definition.pbir and definition/**."""
    pbir_dir = Path(pbir_dir)
    pbir = pbir_dir / "definition.pbir"
    if not pbir.is_file():
        raise InvalidDefinition(f"definition.pbir not found in {pbir_dir}")
    if not (pbir_dir / "definition").is_dir():
        raise InvalidDefinition(f"definition folder not found in {pbir_dir}")
    files = [pbir] + sorted(p for p in (pbir_dir / "definition").rglob("*") if p.is_file())
    return [
        {
            "path": f.relative_to(pbir_dir).as_posix(),
            "payload": base64.b64encode(f.read_bytes()).decode("ascii"),
            "payloadType": "InlineBase64",
        }
        for f in files
    ]


def _body(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class FabricItemsClient:
    def __init__(
        self,
        token_provider: Callable[[], str] | None = None,
        http: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        allow_any_workspace: bool = False,
    ):
        self._token = token_provider or (lambda: get_token(FABRIC_SCOPE))
        self._http = http or (lambda *a, **kw: requests.request(*a, **kw))
        self._sleep = sleep
        self._clock = clock
        self._allow_any = allow_any_workspace

    def _check_target(self, workspace_id: str) -> None:
        if self._allow_any:
            return
        sandbox = get_sandbox_workspace_id()
        if not sandbox:
            raise ConfigError(
                "SANDBOX_WORKSPACE_ID is not set. Deployments are only allowed to the sandbox workspace."
            )
        if workspace_id.strip().casefold() != sandbox.casefold():
            raise TargetNotAllowed(
                "Deployment is only allowed to the configured sandbox workspace"
            )

    def _call(self, method: str, url: str, json: dict | None = None, params: dict | None = None):
        retries = 0
        while True:
            try:
                response = self._http(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {self._token()}"},
                    json=json,
                    params=params,
                    timeout=HTTP_TIMEOUT,
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
                if retries >= MAX_RETRIES_429:
                    raise FabricRateLimited("Fabric is throttling requests", wait, request_id)
                retries += 1
                self._sleep(wait)
                continue
            if status in (401, 403):
                raise FabricForbidden("Fabric denied access", request_id)
            if status == 404:
                raise FabricNotFound("Fabric resource not found", request_id)
            if status >= 500:
                raise FabricUpstreamError(f"Fabric returned HTTP {status}", request_id)
            if status >= 400:
                body = _body(response)
                message = body.get("message") or f"Fabric rejected the request (HTTP {status})"
                raise FabricOperationFailed(message, request_id, body)
            return response

    def _list_items(self, workspace_id: str) -> list[dict]:
        url = f"{FABRIC_API}/workspaces/{workspace_id}/items"
        items: list[dict] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            response = self._call("GET", url, params={"continuationToken": token} if token else None)
            body = _body(response)
            items.extend(body.get("value", []))
            token = body.get("continuationToken")
            if not token:
                return items
            if token in seen:
                raise FabricUpstreamError("Fabric returned a repeating page token")
            seen.add(token)
        raise FabricUpstreamError("Fabric returned too many pages")

    @staticmethod
    def _named(items: list[dict], display_name: str) -> list[dict]:
        wanted = display_name.casefold()
        return [i for i in items if str(i.get("displayName", "")).casefold() == wanted]

    def get_item_by_name(self, workspace_id: str, display_name: str, item_type: str) -> dict | None:
        self._check_target(workspace_id)
        for item in self._named(self._list_items(workspace_id), display_name):
            if item.get("type") == item_type:
                return item
        return None

    def wait_for_operation(self, location_url: str, timeout_s: int = 600) -> dict:
        deadline = self._clock() + timeout_s
        while True:
            response = self._call("GET", location_url)
            body = _body(response)
            status = body.get("status")
            if status == "Succeeded":
                return body
            if status == "Failed":
                error = body.get("error") or {}
                code = error.get("errorCode") or error.get("code") or "Unknown"
                message = error.get("message") or "The operation failed"
                raise FabricOperationFailed(
                    f"{code}: {message}", _request_id(response), error or body
                )
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise FabricTimeout(
                    "The Fabric operation did not finish in time; its state is unknown"
                )
            try:
                wait = float(response.headers.get("Retry-After", DEFAULT_POLL_WAIT))
            except (TypeError, ValueError):
                wait = DEFAULT_POLL_WAIT
            self._sleep(min(max(wait, 1), MAX_RETRY_WAIT, remaining))

    def _finish(self, response) -> None:
        if response.status_code == 202:
            location = response.headers.get("Location")
            if not location:
                raise FabricUpstreamError(
                    "Fabric accepted the request without an operation location",
                    _request_id(response),
                )
            self.wait_for_operation(location)

    def _upsert(self, workspace_id: str, display_name: str, item_type: str, collection: str, definition: dict, label: str) -> dict:
        """Create the item, or update its definition if one with that name exists (any case)."""
        self._check_target(workspace_id)
        base = f"{FABRIC_API}/workspaces/{workspace_id}/{collection}"

        same_name = self._named(self._list_items(workspace_id), display_name)
        for item in same_name:
            if item.get("type") != item_type:
                raise NameConflict(display_name, str(item.get("type", "unknown")))
        if same_name:
            item_id = same_name[0]["id"]
            response = self._call("POST", f"{base}/{item_id}/updateDefinition", {"definition": definition})
            self._finish(response)
            return {"item_id": item_id, "operation": "updated"}

        response = self._call("POST", base, {"displayName": display_name, "definition": definition})
        self._finish(response)
        item_id = _body(response).get("id") if response.status_code != 202 else None
        if not item_id:
            created = self.get_item_by_name(workspace_id, display_name, item_type)
            if not created:
                raise FabricUpstreamError(
                    f"The {label} was created but could not be found in the workspace",
                    _request_id(response),
                )
            item_id = created["id"]
        return {"item_id": item_id, "operation": "created"}

    def upsert_semantic_model(self, workspace_id: str, display_name: str, tmdl_dir: Path) -> dict:
        self._check_target(workspace_id)
        return self._upsert(
            workspace_id, display_name, SEMANTIC_MODEL, "semanticModels", {"parts": tmdl_parts(tmdl_dir)}, "model"
        )

    def upsert_report(self, workspace_id: str, display_name: str, pbir_dir: Path) -> dict:
        """Create or update a report (PBIR folder) in the sandbox workspace. Returns
        {"item_id", "operation": "created"|"updated"}; a repeat leaves one report."""
        self._check_target(workspace_id)
        return self._upsert(
            workspace_id, display_name, REPORT, "reports", {"format": "PBIR", "parts": pbir_parts(pbir_dir)}, "report"
        )

    def upsert_notebook(self, workspace_id: str, display_name: str, ipynb: Path) -> dict:
        """Create or update a notebook (ipynb format) in the sandbox workspace."""
        self._check_target(workspace_id)
        payload = base64.b64encode(Path(ipynb).read_bytes()).decode("ascii")
        definition = {
            "format": "ipynb",
            "parts": [{"path": "notebook-content.ipynb", "payload": payload, "payloadType": "InlineBase64"}],
        }
        return self._upsert(workspace_id, display_name, NOTEBOOK, "notebooks", definition, "notebook")

    def run_notebook(self, workspace_id: str, item_id: str, timeout_s: int = 900) -> dict:
        """Run a notebook and wait for it. Returns {"run_id"}; a run that doesn't succeed raises
        NotebookRunFailed with the run id, and a run that outlasts the timeout is a FabricTimeout."""
        self._check_target(workspace_id)
        url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/jobs/instances"
        response = self._call("POST", f"{url}?jobType=RunNotebook", {})
        location = response.headers.get("Location")
        if not location:
            raise FabricUpstreamError("Fabric accepted the notebook run without a location", _request_id(response))
        run_id = location.rstrip("/").rsplit("/", 1)[-1]
        deadline = self._clock() + timeout_s
        while True:
            polled = self._call("GET", location)
            body = _body(polled)
            status = body.get("status")
            if status == "Completed":
                return {"run_id": body.get("id") or run_id}
            if status in ("Failed", "Cancelled", "Deduped"):
                reason = body.get("failureReason") or {}
                detail = reason.get("message") or status
                raise NotebookRunFailed(
                    f"The notebook run {body.get('id') or run_id} {str(status).lower()}: {detail}",
                    _request_id(polled),
                    reason or body,
                    body.get("id") or run_id,
                )
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise FabricTimeout(f"The notebook run {run_id} did not finish in time; its state is unknown")
            try:
                wait = float(polled.headers.get("Retry-After", DEFAULT_POLL_WAIT))
            except (TypeError, ValueError):
                wait = DEFAULT_POLL_WAIT
            self._sleep(min(max(wait, 1), MAX_RETRY_WAIT, remaining))

    # --- promotion (T-702): back up, restore and delete -----------------------------------------------

    def get_definition(self, workspace_id: str, item_id: str, collection: str) -> list[dict]:
        """The definition parts of an item (`semanticModels` or `reports`), for a backup. A report is asked
        for in PBIR format."""
        self._check_target(workspace_id)
        url = f"{FABRIC_API}/workspaces/{workspace_id}/{collection}/{item_id}/getDefinition"
        if collection == "reports":
            url += "?format=PBIR"
        response = self._call("POST", url, {})
        body = _body(response)
        if response.status_code == 202:
            location = response.headers.get("Location")
            if not location:
                raise FabricUpstreamError("Fabric accepted getDefinition without an operation location", _request_id(response))
            self.wait_for_operation(location)
            body = _body(self._call("GET", location.rstrip("/") + "/result"))
        parts = (body.get("definition") or {}).get("parts")
        if not isinstance(parts, list) or not parts:
            raise FabricUpstreamError("Fabric returned no definition for the item", _request_id(response))
        return parts

    def update_definition(self, workspace_id: str, item_id: str, collection: str, parts: list[dict], fmt: str | None = None) -> None:
        """Replace an item's definition with `parts` (as returned by get_definition): a restore."""
        self._check_target(workspace_id)
        definition: dict = {"parts": parts}
        if fmt:
            definition["format"] = fmt
        response = self._call("POST", f"{FABRIC_API}/workspaces/{workspace_id}/{collection}/{item_id}/updateDefinition", {"definition": definition})
        self._finish(response)

    def delete_item(self, workspace_id: str, item_id: str) -> None:
        """Delete an item: the rollback of an item the deployment created."""
        self._check_target(workspace_id)
        self._call("DELETE", f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}")

    # --- Nexora home workspace (T-005) --------------------------------------------------

    def create_workspace(self, display_name: str) -> dict:
        """Create a workspace. Not subject to `_check_target`: this is how a target
        workspace comes to exist in the first place."""
        response = self._call("POST", f"{FABRIC_API}/workspaces", {"displayName": display_name})
        body = _body(response)
        return {"id": body["id"], "display_name": body.get("displayName", display_name)}

    def get_workspace(self, workspace_id: str) -> dict:
        response = self._call("GET", f"{FABRIC_API}/workspaces/{workspace_id}")
        body = _body(response)
        return {
            "id": body.get("id", workspace_id),
            "display_name": body.get("displayName", ""),
            "capacity_id": body.get("capacityId"),
        }

    @staticmethod
    def _parts_hash(parts: list[dict]) -> str:
        digest = hashlib.sha256()
        for part in sorted(parts, key=lambda p: p["path"]):
            digest.update(part["path"].encode("utf-8"))
            digest.update(part.get("payload", "").encode("utf-8"))
        return digest.hexdigest()

    def upsert_item(
        self,
        workspace_id: str,
        display_name: str,
        item_type: str,
        definition: dict | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create or update a generic item (one without a dedicated `_upsert` helper
        above): Lakehouse, SQLDatabase, Eventhouse, Environment, UserDataFunctions,
        VariableLibrary. Returns `{"item_id", "operation": "created"|"updated"|"unchanged"}`;
        "unchanged" when the item already exists and either has no `definition` to
        compare (most data items) or an unchanged one. `dry_run=True` only reads: it
        reports what would happen without creating, updating or publishing anything."""
        self._check_target(workspace_id)
        same_name = self._named(self._list_items(workspace_id), display_name)
        for item in same_name:
            if item.get("type") != item_type:
                raise NameConflict(display_name, str(item.get("type", "unknown")))

        if same_name:
            item_id = same_name[0]["id"]
            if definition is None:
                return {"item_id": item_id, "operation": "unchanged"}
            try:
                existing_parts = self.get_definition(workspace_id, item_id, "items")
                changed = self._parts_hash(existing_parts) != self._parts_hash(definition.get("parts", []))
            except Exception:
                changed = True
            if not changed:
                return {"item_id": item_id, "operation": "unchanged"}
            if dry_run:
                return {"item_id": item_id, "operation": "updated"}
            response = self._call(
                "POST",
                f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/updateDefinition",
                {"definition": definition},
            )
            self._finish(response)
            return {"item_id": item_id, "operation": "updated"}

        if dry_run:
            return {"item_id": None, "operation": "created"}

        body: dict = {"displayName": display_name, "type": item_type}
        if definition is not None:
            body["definition"] = definition
        response = self._call("POST", f"{FABRIC_API}/workspaces/{workspace_id}/items", body)
        self._finish(response)
        item_id = _body(response).get("id") if response.status_code != 202 else None
        if not item_id:
            created = self.get_item_by_name(workspace_id, display_name, item_type)
            if not created:
                raise FabricUpstreamError(
                    f"The {item_type} item was created but could not be found in the workspace",
                    _request_id(response),
                )
            item_id = created["id"]
        return {"item_id": item_id, "operation": "created"}

    def upload_environment_library(self, workspace_id: str, environment_id: str, wheel_path: Path) -> None:
        """Upload `wheel_path` as a custom library to the Environment's staging area."""
        self._check_target(workspace_id)
        url = f"{FABRIC_API}/workspaces/{workspace_id}/environments/{environment_id}/staging/libraries"
        with open(wheel_path, "rb") as handle:
            response = self._http(
                "POST",
                url,
                headers={"Authorization": f"Bearer {self._token()}"},
                files={"file": (wheel_path.name, handle, "application/octet-stream")},
                timeout=HTTP_TIMEOUT,
            )
        if response.status_code >= 400:
            body = _body(response)
            message = body.get("message") or f"Fabric rejected the library upload (HTTP {response.status_code})"
            raise FabricOperationFailed(message, _request_id(response), body)

    def publish_environment(self, workspace_id: str, environment_id: str) -> None:
        """Publish the Environment's staged content (the uploaded wheel becomes active)."""
        self._check_target(workspace_id)
        url = f"{FABRIC_API}/workspaces/{workspace_id}/environments/{environment_id}/staging/publish"
        response = self._call("POST", url, {})
        self._finish(response)
