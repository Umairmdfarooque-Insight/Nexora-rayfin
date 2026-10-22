"""Nexora orchestration API. Start: uvicorn app.api.main:app --reload --port 8000

Every endpoint except /health needs an Entra bearer token (T-105).
"""

import logging
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import fabric, me, runs
from app.api.errors import register_error_handlers
from app.api.schemas import Health
from app.compiler.critical_terms import load_critical_terms
from app.config import APP_NAME, PROJECT_ROOT
from app.observability import telemetry
from app.security.startup import assert_no_external_tracing
from app.observability.http import install as install_http_correlation
from app.observability.logs import configure_logging
from app.observability.middleware import correlation
from app.workflow.roles import load_roles

RAYFIN_ORIGIN = "http://localhost:3000"


def _version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "0.0.0"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "0.0.0"


VERSION = _version()

# The app's own loggers write JSON lines (ts, level, run_id, stage, msg, ...) at INFO; third-party libraries
# stay quiet. Telemetry (T-704) starts from OTEL_EXPORTER and never stops the API if it can't.
configure_logging()
telemetry.init_from_env()
install_http_correlation()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_critical_terms()  # a missing or invalid config/critical_terms.yaml stops the API here
    load_roles()  # so does config/roles.yaml
    assert_no_external_tracing()  # and a LangSmith tracing setting (T-705)
    yield


app = FastAPI(title=f"{APP_NAME} API", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[RAYFIN_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(correlation)
register_error_handlers(app)
app.include_router(runs.router)
app.include_router(fabric.router)
app.include_router(me.router)


@app.get("/health", response_model=Health, tags=["health"])
def health() -> Health:
    return Health(status="ok", app=APP_NAME, version=VERSION)
