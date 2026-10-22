"""Startup checks (T-705)."""

import os

from app.compiler.critical_terms import ConfigError

# LangSmith would send prompts and metadata outside the tenant (CONTRACTS.md#stage-graphs, rule 6, NFR-008).
FORBIDDEN_TRACING = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_TRACING_V2")


def assert_no_external_tracing() -> None:
    """Raise ConfigError naming the setting if LangSmith tracing is on. The API calls it at startup and
    the shared LLM client when it loads, so no process can send prompts to LangSmith."""
    for name in FORBIDDEN_TRACING:
        if os.getenv(name, "").strip().lower() in ("true", "1", "yes", "on"):
            raise ConfigError(
                f"CONFIG_ERROR: {name} is true. LangSmith tracing would send prompts and metadata outside the "
                f"tenant, so it is not allowed. Remove {name} (or set it to false) and start again."
            )
