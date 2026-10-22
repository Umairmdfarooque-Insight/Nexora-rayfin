"""Push telemetry rows into the home workspace's Eventhouse (T-704, `OTEL_EXPORTER=eventhouse`).

`EventhouseSink.send(table, rows)` posts a batch of JSON rows to `NEXORA_EVENTHOUSE_URI`: an Eventstream
custom endpoint that lands them in the KQL database `NEXORA_EVENTHOUSE_DATABASE` (tables `nexora_logs`,
`nexora_spans`, `nexora_metrics`). The exact shape an Eventstream custom endpoint expects, and which token
audience it accepts, are **not yet confirmed against a live tenant** (see fabric-hosting.md's
platform-limits table) — this is a best-effort implementation from public docs, the same caveat as
T-101's `OneLakeArtifactStore`. A direct Kusto ingestion endpoint (`https://<cluster>.kusto.windows.net`)
is the documented alternative if the Eventstream shape turns out to be wrong; `EventhouseSink` only needs
a URI and a token to send to, so switching is a matter of changing `NEXORA_EVENTHOUSE_URI` and the scope
below, not this module's callers.

Never raises into the caller: a telemetry sink that is down must not break a run. Callers (`app.observability
.telemetry`) catch everything anyway, but this module logs its own failure once so the cause is visible.
"""

import logging
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

_warned = False


class EventhouseSink:
    def __init__(self, uri: str, database: str, token_provider: Callable[[], str] | None = None, timeout: float = 10.0):
        self.uri = uri.rstrip("/")
        self.database = database
        self.timeout = timeout
        self._token_provider = token_provider

    def _token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        from app.auth import KUSTO_SCOPE, get_token

        return get_token(KUSTO_SCOPE)

    def send(self, table: str, rows: list[dict[str, Any]]) -> None:
        """POST one batch of rows for `table`. Swallows its own failure (one warning per process)."""
        global _warned
        if not rows:
            return
        try:
            response = requests.post(
                self.uri,
                json={"database": self.database, "table": table, "rows": rows},
                headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            if not _warned:
                _warned = True
                logger.warning("Couldn't send telemetry to the Eventhouse at %s: %s", self.uri, exc)


def reset_for_tests() -> None:
    global _warned
    _warned = False
