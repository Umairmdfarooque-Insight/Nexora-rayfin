"""The correlation ID on outbound HTTP calls (T-704): `x-nexora-run-id` on every `requests` call made while a
run is being worked on. Fabric, Power BI and OneLake calls all go through `requests`; the model calls add
the same header themselves. Installed once; calls outside a run are untouched."""

import requests

from app.observability.context import current_run_id

HEADER = "x-nexora-run-id"
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    original = requests.sessions.Session.request

    def request(self, method, url, **kwargs):  # noqa: ANN001
        run_id = current_run_id()
        if run_id:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault(HEADER, run_id)
            kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = request  # type: ignore[method-assign]
    _installed = True
