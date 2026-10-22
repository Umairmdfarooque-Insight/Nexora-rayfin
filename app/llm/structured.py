"""Schema-constrained LLM calls (Responses API, JSON Schema structured output)."""

import openai

from app.observability import telemetry
from app.observability.context import current_run_id

from app.config import AZURE_OPENAI_DEPLOYMENT
from app.llm.client import client
from app.llm.errors import LlmTimeout, LlmUpstreamError, PromptRejected

TIMEOUT_SECONDS = 60
_FILTER_MARKERS = ("content_filter", "responsibleaipolicyviolation", "content management policy")


def ask_structured(system: str, user: str, name: str, schema: dict) -> str:
    """Return the model's raw JSON text for `schema` (strict). Errors are typed."""
    try:
        response = client.responses.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            instructions=system,
            input=user,
            text={
                "format": {
                    "type": "json_schema",
                    "name": name,
                    "schema": schema,
                    "strict": True,
                }
            },
            timeout=TIMEOUT_SECONDS,
            extra_headers={"x-nexora-run-id": current_run_id()} if current_run_id() else None,
        )
    except openai.APITimeoutError:
        raise LlmTimeout(
            f"The model did not answer within {TIMEOUT_SECONDS} seconds"
        ) from None
    except openai.BadRequestError as exc:
        if any(marker in str(exc).lower() for marker in _FILTER_MARKERS):
            raise PromptRejected(
                "The request was rejected by the content filter. Rephrase the requirement."
            ) from None
        raise LlmUpstreamError("The model service rejected the request") from None
    except openai.OpenAIError:
        raise LlmUpstreamError("The model service returned an error") from None
    output = response.output_text or ""
    usage = getattr(response, "usage", None)
    telemetry.record_llm(
        AZURE_OPENAI_DEPLOYMENT, system, user, output,
        getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
    )
    return output
