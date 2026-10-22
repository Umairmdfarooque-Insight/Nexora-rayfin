"""The LLM payload guard (T-705, NFR-008): only shapes the model is meant to see go out.

What may be sent: the prompt, the contract, names and types of tables and columns, and profile
summaries (counts, min and max). What may not: row-like data, that is, source values. The guard runs in
the shared LLM client (`app/llm/client.py`), so no code path can skip it, stage graph nodes included.

A payload is rejected (`PayloadRejected`, nothing is sent) if it holds:
- a list of 5 or more dictionaries with the same keys that aren't metadata keys (a list of rows);
- a list of 5 or more lists of plain values (a matrix of rows);
- a field named `rows`, `records`, `samples` and the like with content;
- many consecutive lines of delimiter-separated values (a pasted table);
- more than MAX_CHARS characters.

JSON inside a text (the model's `input` is mostly JSON) is parsed and checked the same way. The size and
shape of every payload that passes, and the reason for every one that doesn't, are logged (never the
content): see `describe`.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MIN_ROWS = 5
MAX_CHARS = 400_000
MIN_TABLE_LINES = 10
# Keys of the dictionaries the pipeline sends in lists: tables, columns, KPIs, measures, relationships,
# questions. A list of 5 or more dictionaries is a list of rows unless its keys are all among these.
METADATA_KEYS = frozenset(
    {
        "name", "type", "data_type", "table", "column", "columns", "role", "grain", "model_table", "source", "model_column",
        "source_column", "id", "text", "kpi", "format_string", "description", "formula_intent", "filters", "time_logic",
        "exclusions", "currency", "levels", "hierarchies", "schema", "nullable", "entity", "need", "mapped_to",
    }
)
ROW_FIELDS = frozenset({"rows", "records", "row_data", "sample_rows", "samples", "sample_values", "values_sample", "data_rows"})
SCALARS = (str, int, float, bool, type(None))


class PayloadRejected(Exception):
    """The payload holds something that must not reach the model. Nothing was sent."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The model payload was rejected: {reason}")


def _rows_in_list(items: list, path: str) -> str | None:
    if len(items) < MIN_ROWS:
        return None
    if all(isinstance(i, dict) for i in items):
        keysets = {frozenset(i) for i in items}
        if len(keysets) == 1 and not next(iter(keysets)) <= METADATA_KEYS:
            return f"{path} is a list of {len(items)} records with data columns ({', '.join(sorted(next(iter(keysets)))[:5])}), which looks like rows"
    if all(isinstance(i, (list, tuple)) and i and all(isinstance(v, SCALARS) for v in i) for i in items):
        return f"{path} is a list of {len(items)} rows of values"
    return None


def _table_lines(text: str, path: str) -> str | None:
    """Delimiter-separated lines in a row (a table pasted into free text)."""
    run, last = 0, None
    for line in text.splitlines():
        counts = tuple(line.count(d) for d in (",", "\t", "|", ";"))
        count = max(counts)
        if count >= 2 and count == last:
            run += 1
            if run + 1 >= MIN_TABLE_LINES:
                return f"{path} holds {run + 1} lines of delimiter-separated values, which looks like a pasted table"
        else:
            run = 0
        last = count if count >= 2 else None
    return None


def _walk(value: Any, path: str, depth: int = 0) -> str | None:
    if depth > 40:
        return f"{path} is nested too deeply"
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in ROW_FIELDS and isinstance(item, (list, tuple, dict)) and item:
                return f"{path}.{key} holds data rows"
            found = _walk(item, f"{path}.{key}", depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        found = _rows_in_list(list(value), path)
        if found:
            return found
        for i, item in enumerate(value):
            found = _walk(item, f"{path}[{i}]", depth + 1)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[" and len(text) > 1:
            try:
                return _walk(json.loads(text), path, depth + 1)
            except ValueError:
                pass
        for paragraph in re.split(r"\n\s*\n", value):  # `<json>\n\nmore text`
            p = paragraph.strip()
            if p[:1] in "{[" and p != text:
                try:
                    found = _walk(json.loads(p), path, depth + 1)
                except ValueError:
                    continue
                if found:
                    return found
        return _table_lines(value, path)
    return None


def describe(value: Any, depth: int = 0) -> Any:
    """The shape of a payload with sizes only: `{"tables": "list[41]", "contract": {"kpis": "list[2]"}}`."""
    if isinstance(value, dict):
        return {k: (describe(v, depth + 1) if depth < 1 and isinstance(v, dict) else _kind(v)) for k, v in value.items()}
    return _kind(value)


def _kind(value: Any) -> str:
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    if isinstance(value, str):
        return f"str[{len(value)}]"
    return type(value).__name__


def size_of(payload: dict) -> int:
    return len(json.dumps(payload, default=str, ensure_ascii=False))


def check_payload(payload: dict) -> None:
    """Raise PayloadRejected(reason) if `payload` (a dict of what is about to be sent, for example
    `{"instructions": ..., "input": ...}`) holds rows or is too large. Logs the size and shape of every
    payload; the reason, never the content, of a rejected one."""
    size = size_of(payload)
    shape = describe(payload)
    reason = f"the payload is {size:,} characters; the limit is {MAX_CHARS:,}" if size > MAX_CHARS else _walk(payload, "payload")
    if reason:
        logger.warning("model payload rejected", extra={"payload_chars": size, "payload_shape": shape, "reason": reason})
        raise PayloadRejected(reason)
    logger.info("model payload", extra={"payload_chars": size, "payload_shape": shape})
