"""Deterministic ambiguity rules for the Requirement Compiler (FR-002).

These run after the LLM on every path, so a business-critical term that appears without a
definition always becomes a blocking question, whatever the model said.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app import config_files
from app.config import PROJECT_ROOT
from app.contracts.business_contract import (
    BusinessContract,
    LockedFact,
    OpenQuestion,
    QuestionSource,
    QuestionStatus,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "critical_terms.yaml"
METRIC_QUESTION_ID = "RULE-METRIC"
LLM_ID_PREFIX = "LLM-"  # temporary ids of LLM questions until apply_locked_facts renumbers them


class ConfigError(Exception):
    """A configuration file is missing or invalid. The message names the file."""


@dataclass(frozen=True)
class CriticalTerms:
    terms: dict[str, list[str]]
    definition_patterns: list[str]


def load_critical_terms(path: Path = CONFIG_PATH) -> CriticalTerms:
    label = path.name
    from_fabric = path is CONFIG_PATH and config_files.from_fabric()
    try:
        text = config_files.read_text(label) if from_fabric else path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file {label} is missing ({path})") from None
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"Configuration file {label} can't be read: {exc}") from None
    except Exception as exc:  # the OneLake connector: a missing file, a network error, …
        if not from_fabric:
            raise
        raise ConfigError(f"Configuration file {label} is missing or can't be read from Files/config/: {exc}") from None

    def bad(reason: str) -> ConfigError:
        return ConfigError(f"Configuration file {label} is invalid: {reason}")

    if not isinstance(data, dict):
        raise bad("expected a mapping with `terms` and `definition_patterns`")
    terms = data.get("terms")
    patterns = data.get("definition_patterns")
    if not isinstance(terms, dict) or not terms:
        raise bad("`terms` must be a non-empty mapping of term to aliases")
    clean: dict[str, list[str]] = {}
    for term, aliases in terms.items():
        if not isinstance(term, str) or not term.strip():
            raise bad("term names must be non-empty text")
        if not isinstance(aliases, list) or not all(
            isinstance(a, str) and a.strip() for a in aliases
        ):
            raise bad(f"aliases of {term!r} must be a list of text")
        clean[term.strip()] = [a.strip() for a in aliases]
    if (
        not isinstance(patterns, list)
        or not patterns
        or not all(isinstance(p, str) and "{term}" in p for p in patterns)
    ):
        raise bad("`definition_patterns` must be a non-empty list of text containing {term}")
    return CriticalTerms(terms=clean, definition_patterns=patterns)


def _flexible(text: str) -> str:
    """A regex for `text` that ignores whitespace differences.

    Whitespace between two letters or digits must be present; next to punctuation
    (as in "revenue =") it is optional.
    """
    pieces = [part for part in re.split(r"(\s+)", text) if part]
    out: list[str] = []
    for index, piece in enumerate(pieces):
        if not piece.isspace():
            out.append(re.escape(piece))
            continue
        left = pieces[index - 1] if index > 0 else ""
        right = pieces[index + 1] if index + 1 < len(pieces) else ""
        strict = bool(left) and bool(right) and left[-1].isalnum() and right[0].isalnum()
        out.append(r"\s+" if strict else r"\s*")
    return "".join(out)


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<!\w){_flexible(name)}(?!\w)", text, re.IGNORECASE) is not None


def _defined(text: str, names: list[str], patterns: list[str]) -> bool:
    for name in names:
        for pattern in patterns:
            regex = _flexible(pattern.replace("{term}", name))
            if re.search(rf"(?<!\w){regex}", text, re.IGNORECASE):
                return True
    return False


def term_question_id(term: str) -> str:
    return "RULE-" + re.sub(r"[^A-Z0-9]+", "-", term.upper()).strip("-")


def _answered(previous: BusinessContract | None, question_id: str) -> bool:
    if previous is None:
        return False
    answered = any(
        q.id == question_id and q.status is QuestionStatus.ANSWERED
        for q in previous.open_questions
    )
    locked = any(f.source_question_id == question_id for f in previous.locked_facts)
    return answered or locked


def detect_undefined_terms(text: str, config: CriticalTerms) -> list[str]:
    """Critical terms that appear in `text` without a definition."""
    found = []
    for term, aliases in config.terms.items():
        names = [term, *aliases]
        if any(_mentions(text, n) for n in names) and not _defined(
            text, names, config.definition_patterns
        ):
            found.append(term)
    return found


def apply_critical_terms(
    contract: BusinessContract,
    text: str,
    previous: BusinessContract | None,
    config: CriticalTerms,
) -> BusinessContract:
    """Add a blocking question per undefined critical term, and one when no KPI is named.

    A term whose question was answered earlier (it is in `previous`) counts as defined.
    Rule questions replace any LLM question with the same id, so the LLM can't drop them.
    """
    required: list[OpenQuestion] = []
    for term in detect_undefined_terms(text, config):
        question_id = term_question_id(term)
        if _answered(previous, question_id):
            continue
        required.append(
            OpenQuestion(
                id=question_id,
                text=f"How is {term} defined?",
                blocking=True,
                reason="Business-critical term without definition",
                source=QuestionSource.RULE,
                status=QuestionStatus.OPEN,
            )
        )
    if not contract.kpis and not _answered(previous, METRIC_QUESTION_ID):
        required.append(
            OpenQuestion(
                id=METRIC_QUESTION_ID,
                text="Which metric should be measured?",
                blocking=True,
                reason="The requirement names no measurable KPI",
                source=QuestionSource.RULE,
                status=QuestionStatus.OPEN,
            )
        )
    rule_ids = {q.id for q in required}
    # An LLM question that asks about a term a rule question already covers is a duplicate.
    covered = [
        [term, *config.terms[term]]
        for term in config.terms
        if term_question_id(term) in rule_ids
    ]
    kept = [
        q
        for q in contract.open_questions
        if q.id not in rule_ids
        and not (
            q.id.startswith(LLM_ID_PREFIX)
            and any(_mentions(q.text, n) for names in covered for n in names)
        )
    ]
    return _rebuild(contract, open_questions=[*required, *kept])


def _normal(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def term_for_fact(fact: LockedFact, config: CriticalTerms) -> str | None:
    """The critical term a locked fact defines: the fact came from that term's rule question."""
    for term in config.terms:
        if fact.source_question_id == term_question_id(term):
            return term
    return None


def matching_kpi_index(contract: BusinessContract, term: str, config: CriticalTerms) -> int | None:
    """Index of the first KPI whose name mentions the term or one of its aliases."""
    names = [term, *config.terms[term]]
    for index, kpi in enumerate(contract.kpis):
        if any(_mentions(kpi.name, n) for n in names):
            return index
    return None


def _enforce_term_facts(
    contract: BusinessContract, config: CriticalTerms, warnings: list[str]
) -> BusinessContract:
    """A locked fact that defines a critical term is the KPI's `formula_intent`, whatever
    the LLM wrote. The first KPI that mentions the term gets it."""
    kpis = [k.model_copy() for k in contract.kpis]
    changed = False
    for fact in contract.locked_facts:
        term = term_for_fact(fact, config)
        index = matching_kpi_index(contract, term, config) if term else None
        if index is None or kpis[index].formula_intent == fact.text:
            continue
        if (kpis[index].formula_intent or "").strip():
            warnings.append(
                f"LLM output contradicts locked fact {fact.id}; "
                f"formula_intent of KPI {kpis[index].name!r} was reset to the locked fact"
            )
        kpis[index] = kpis[index].model_copy(update={"formula_intent": fact.text})
        changed = True
    if not changed:
        return contract
    data = contract.model_dump(mode="json")
    data["kpis"] = [k.model_dump(mode="json") for k in kpis]
    return BusinessContract.model_validate(data)


def apply_locked_facts(
    contract: BusinessContract,
    previous: BusinessContract | None,
    config: CriticalTerms | None = None,
    warnings: list[str] | None = None,
) -> BusinessContract:
    """Carry earlier answers, dismissals and locked facts into the new contract unchanged.

    LLM questions get their final ids (Q1, Q2, …) here. One that repeats the text of
    another question is dropped. With `config`, a fact that defines a critical term is
    re-applied to its KPI; each override is appended to `warnings`.
    """
    questions = list(contract.open_questions)
    facts = list(contract.locked_facts)
    if previous is not None:
        have = {q.id for q in questions}
        for question in previous.open_questions:
            # Answered and dismissed questions stay closed; an open question a reviewer
            # raised (T-204) stays open until someone answers it.
            keep = (
                question.status is not QuestionStatus.OPEN
                or question.source is QuestionSource.REVIEWER
            )
            if keep and question.id not in have:
                questions.append(question)
                have.add(question.id)
        have_facts = {f.id for f in facts}
        for fact in previous.locked_facts:
            if fact.id not in have_facts:
                facts.append(fact)

    fixed = [q for q in questions if not q.id.startswith(LLM_ID_PREFIX)]
    taken = {q.id for q in fixed}
    seen_text = {_normal(q.text) for q in fixed}
    counter = 0
    final: list[OpenQuestion] = []
    for question in questions:
        if not question.id.startswith(LLM_ID_PREFIX):
            final.append(question)
            continue
        if _normal(question.text) in seen_text:
            continue
        seen_text.add(_normal(question.text))
        counter += 1
        while f"Q{counter}" in taken:
            counter += 1
        taken.add(f"Q{counter}")
        final.append(question.model_copy(update={"id": f"Q{counter}"}))
    result = _rebuild(contract, open_questions=final, locked_facts=facts)
    if config is not None:
        result = _enforce_term_facts(result, config, warnings if warnings is not None else [])
    return result


def _rebuild(contract: BusinessContract, **changes) -> BusinessContract:
    # Re-validate so the contract rules (unique ids, …) still hold.
    data = contract.model_dump(mode="json")
    for key, items in changes.items():
        data[key] = [item.model_dump(mode="json") for item in items]
    return BusinessContract.model_validate(data)
