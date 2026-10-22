"""Requirement Compiler (M2): turns a run's prompt into a draft BusinessContract.

The LLM proposes a draft; deterministic code validates it and adds the questions the rules
require. The loop around the LLM is the `compile` stage graph (app/graphs/compile.py). This
module never changes run state.
"""

import copy
import json
from functools import lru_cache
from typing import Callable

from app.compiler.critical_terms import LLM_ID_PREFIX
from app.contracts import business_contract as bc
from app.llm.errors import LlmInvalidOutput
from app.workflow.run import Run

DEFAULT_DOMAIN = "Sales"  # initial domain (README, "Initial domain")
SCHEMA_NAME = "business_contract_draft"

# The LLM fills only these. Everything else (ids, versions, status, locked facts,
# feasibility, question sources and answers) is set by code.
_ROOT_EXCLUDED = {"schema_version", "run_id", "version", "status", "locked_facts", "feasibility"}
_DEF_EXCLUDED = {"Kpi": {"status"}, "OpenQuestion": {"id", "source", "status", "answer", "dismiss_reason"}}
# Keywords OpenAI strict structured output doesn't accept.
_UNSUPPORTED = {"default", "title", "minLength", "maxLength", "minimum", "maximum", "format"}

SYSTEM_PROMPT = """You are Nexora's Requirement Compiler for Microsoft Fabric analytics.
Turn the business requirement into a draft business contract, as JSON matching the schema.

Rules:
1. Record only what the requirement states. Use null (or an empty list) for anything it doesn't say.
2. Never invent a KPI definition. Put `formula_intent` only when the requirement gives one, and say it in business terms.
3. Anything you infer goes in `assumptions`, never in KPI fields.
4. Dimensions are what the data is grouped or sliced by, including time periods (for example "by month" gives the dimension Month).
5. Business-critical terms such as Revenue, Margin or Retention are checked by separate rules. Don't ask what they mean; the rules do.
6. Add other open questions only for material gaps. Set `blocking` true only when work can't proceed at all without the answer, for example when the requirement names no metric. Questions about details of a definition the requirement already gives are never blocking.
7. Do not name database tables or columns.
8. If a previous contract is given, keep its content and change only what the requirement or constraints now say differently. Its `locked_facts` and answered questions were confirmed by the business owner: never contradict them.
Return JSON only."""


def _reachable_defs(schema: dict) -> set[str]:
    refs: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[1]
                if name not in refs:
                    refs.add(name)
                    walk(schema["$defs"][name])
            for key, value in node.items():
                if key != "$defs":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk({k: v for k, v in schema.items() if k != "$defs"})
    return refs


def _strictify(node):
    if isinstance(node, dict):
        for key in _UNSUPPORTED:
            node.pop(key, None)
        props = node.get("properties")
        if isinstance(props, dict):
            node["required"] = list(props)
            node["additionalProperties"] = False
        for value in node.values():
            _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)


@lru_cache(maxsize=1)
def _llm_schema_json() -> str:
    schema = copy.deepcopy(bc.json_schema())
    for name in _ROOT_EXCLUDED:
        schema["properties"].pop(name, None)
    for def_name, fields in _DEF_EXCLUDED.items():
        for field in fields:
            schema["$defs"][def_name]["properties"].pop(field, None)
    keep = _reachable_defs(schema)
    schema["$defs"] = {k: v for k, v in schema["$defs"].items() if k in keep}
    schema.pop("required", None)
    _strictify(schema)
    return json.dumps(schema)


def llm_schema() -> dict:
    """The JSON Schema sent to the LLM: T-201's schema, limited to what the LLM may fill
    and made strict (every property required, no extra properties)."""
    return json.loads(_llm_schema_json())


def build_user_prompt(
    run: Run,
    previous: bc.BusinessContract | None,
    errors: list[str],
    guidance: str | None = None,
) -> str:
    """The only data sent to the LLM: prompt, subject area, outputs, constraints and the
    previous contract. No source data (NFR-008)."""
    parts = [f"Business requirement:\n{run.prompt}"]
    if run.subject_area:
        parts.append(f"Subject area: {run.subject_area}")
    if run.desired_outputs:
        parts.append("Desired outputs: " + ", ".join(run.desired_outputs))
    if run.constraints:
        parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in run.constraints))
    if previous is not None:
        parts.append("Previous contract (YAML):\n" + bc.to_yaml(previous))
    if guidance:
        parts.append(
            "Reviewer guidance for this redraft (follow it unless it contradicts a locked fact):\n"
            + guidance
        )
    if errors:
        parts.append(
            "Your previous output was rejected. Fix these problems and answer again:\n"
            + "\n".join(f"- {e}" for e in errors)
        )
    return "\n\n".join(parts)


def _tidy(values):
    """Capitalise the first letter and drop case-insensitive duplicates, so wording
    differences between runs ("month" and "Month") don't change the contract's structure."""
    if not isinstance(values, list):
        return values
    out: list = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            out.append(value)
            continue
        text = value.strip()
        text = text[:1].upper() + text[1:]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def parse_draft(
    raw: str, run: Run, previous: bc.BusinessContract | None
) -> tuple[bc.BusinessContract | None, list[str]]:
    """Validate the LLM's JSON. Returns the draft contract, or None with the problems."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [f"output is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["output must be a JSON object"]

    data = {k: v for k, v in data.items() if k not in _ROOT_EXCLUDED}
    data.update(
        run_id=run.run_id,
        version=(previous.version + 1) if previous else 1,
        status="draft",
    )
    for field in ("entities", "dimensions"):
        if field in data:
            data[field] = _tidy(data[field])
    if not data.get("domain"):
        data["domain"] = run.subject_area or DEFAULT_DOMAIN
    questions = data.get("open_questions")
    if isinstance(questions, list):
        data["open_questions"] = [
            {**q, "id": f"{LLM_ID_PREFIX}{i}", "source": "llm", "status": "open", "answer": None}
            if isinstance(q, dict)
            else q
            for i, q in enumerate(questions, start=1)
        ]
    contract, issues = bc.check(data)
    return contract, issues


LlmCall = Callable[[str, str, dict], str]


def default_llm() -> LlmCall:
    from app.llm.structured import ask_structured

    return lambda system, user, schema: ask_structured(system, user, SCHEMA_NAME, schema)


def compile_requirement(
    run: Run,
    previous: bc.BusinessContract | None,
    llm: LlmCall | None = None,
    config=None,
    warnings: list[str] | None = None,
    guidance: str | None = None,
) -> bc.BusinessContract:
    """Compile `run.prompt` into a draft contract (version = previous version + 1).

    Warnings (for example a locked fact that had to be re-applied) are appended to `warnings`.

    Raises LlmInvalidOutput if the LLM's output is invalid on both attempts, and
    LlmTimeout, PromptRejected or LlmUpstreamError from the LLM call.
    """
    from app.compiler.critical_terms import load_critical_terms
    from app.graphs.base import run_graph
    from app.graphs.compile import build_compile_graph

    graph = build_compile_graph(llm=llm or default_llm(), config=config or load_critical_terms())
    result = run_graph(
        graph,
        {
            "run": run,
            "previous": previous,
            "attempt": 0,
            "errors": [],
            "attempts_log": [],
            "warnings": [],
            "guidance": guidance,
        },
        run_id=run.run_id,
        stage="compile",
    )
    contract = result.get("contract")
    if contract is None:
        raise LlmInvalidOutput(
            "The model's output was not a valid contract after 2 attempts",
            result.get("attempts_log", []),
        )
    if warnings is not None:
        warnings.extend(result.get("warnings", []))
    return contract
