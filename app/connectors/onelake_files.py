"""Minimal OneLake Files (ADLS Gen2-compatible DFS) read/write, for small
control-plane files: the smoke check's temporary file (T-003) and the config files
`deploy_home` publishes (T-005). Not a general-purpose artifact store — T-101 builds
the `ArtifactStore` that run files go through.
"""

from typing import Callable

import requests

from app.auth import STORAGE_SCOPE, get_token

ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
_TIMEOUT = 30


def _url(workspace_id: str, item_id: str, path: str) -> str:
    return f"{ONELAKE_DFS}/{workspace_id}/{item_id}/{path.lstrip('/')}"


def _headers(token_provider: Callable[[], str] | None) -> dict:
    token = (token_provider or (lambda: get_token(STORAGE_SCOPE)))()
    return {"Authorization": f"Bearer {token}"}


def write_file(
    workspace_id: str,
    item_id: str,
    path: str,
    data: bytes,
    token_provider: Callable[[], str] | None = None,
) -> None:
    """Create (or overwrite) `path` under the item's Files/ root with `data`."""
    headers = _headers(token_provider)
    base = _url(workspace_id, item_id, path)

    create = requests.put(base, params={"resource": "file"}, headers=headers, timeout=_TIMEOUT)
    create.raise_for_status()

    if data:
        append = requests.patch(
            base,
            params={"action": "append", "position": 0},
            headers=headers,
            data=data,
            timeout=_TIMEOUT,
        )
        append.raise_for_status()

    flush = requests.patch(
        base,
        params={"action": "flush", "position": len(data)},
        headers=headers,
        timeout=_TIMEOUT,
    )
    flush.raise_for_status()


def read_file(
    workspace_id: str,
    item_id: str,
    path: str,
    token_provider: Callable[[], str] | None = None,
) -> bytes:
    headers = _headers(token_provider)
    response = requests.get(_url(workspace_id, item_id, path), headers=headers, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.content


def delete_file(
    workspace_id: str,
    item_id: str,
    path: str,
    token_provider: Callable[[], str] | None = None,
) -> None:
    headers = _headers(token_provider)
    response = requests.delete(_url(workspace_id, item_id, path), headers=headers, timeout=_TIMEOUT)
    if response.status_code not in (200, 202, 204, 404):
        response.raise_for_status()


def create_if_absent(
    workspace_id: str,
    item_id: str,
    path: str,
    data: bytes,
    token_provider: Callable[[], str] | None = None,
) -> bool:
    """Create `path` only if it doesn't exist yet (`If-None-Match: *`), for a lock file.
    Returns True if this call created it, False if it already existed."""
    headers = _headers(token_provider) | {"If-None-Match": "*"}
    base = _url(workspace_id, item_id, path)
    create = requests.put(base, params={"resource": "file"}, headers=headers, timeout=_TIMEOUT)
    if create.status_code in (409, 412):
        return False
    create.raise_for_status()
    if data:
        append = requests.patch(
            base,
            params={"action": "append", "position": 0},
            headers=_headers(token_provider),
            data=data,
            timeout=_TIMEOUT,
        )
        append.raise_for_status()
    flush = requests.patch(
        base,
        params={"action": "flush", "position": len(data)},
        headers=_headers(token_provider),
        timeout=_TIMEOUT,
    )
    flush.raise_for_status()
    return True


def list_files(
    workspace_id: str,
    item_id: str,
    directory: str,
    token_provider: Callable[[], str] | None = None,
) -> list[str]:
    """Every file path (relative to the item's Files/ root) under `directory`, recursively.
    An empty list when the directory doesn't exist."""
    headers = _headers(token_provider)
    url = f"{ONELAKE_DFS}/{workspace_id}/{item_id}"
    paths: list[str] = []
    continuation: str | None = None
    while True:
        params = {"resource": "filesystem", "recursive": "true", "directory": directory.strip("/")}
        if continuation:
            params["continuation"] = continuation
        response = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        paths.extend(
            p["name"] for p in body.get("paths", []) if not p.get("isDirectory")
        )
        continuation = response.headers.get("x-ms-continuation")
        if not continuation:
            return paths
