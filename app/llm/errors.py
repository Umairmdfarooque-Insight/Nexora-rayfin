"""Errors from LLM calls, mapped to the error envelope by app/api/errors.py."""


class LlmTimeout(Exception):
    """The model didn't answer within the time limit."""


class PromptRejected(Exception):
    """The content filter rejected the prompt."""


class LlmUpstreamError(Exception):
    """The model service returned an error."""


class LlmInvalidOutput(Exception):
    """The model's output was unusable after the allowed attempts.

    `attempts` holds `{attempt, raw, errors}` per try, saved for debugging.
    """

    def __init__(self, message: str, attempts: list[dict] | None = None):
        self.attempts = attempts or []
        super().__init__(message)
