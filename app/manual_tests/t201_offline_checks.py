"""Offline checks for T-201 (BusinessContract schema).

Run: python -m app.manual_tests.t201_offline_checks
Expected: every check prints PASS and the last line is `All checks passed.` (exit 0).
Uses only files in a temporary folder plus samples/ and schemas/; no network.
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

from app import cli
from app.config import PROJECT_ROOT
from app.contracts import business_contract as bc

SAMPLE = Path(PROJECT_ROOT) / "samples" / "contract_valid.yaml"
SCHEMA_FILE = Path(PROJECT_ROOT) / "schemas" / "business_contract.schema.json"

failures: list[str] = []


class _Capture(io.StringIO):
    def reconfigure(self, **_kwargs) -> None:  # cli.main() switches streams to UTF-8
        pass


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def run_cli(*args: str) -> tuple[int, str]:
    out = _Capture()
    err = _Capture()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue() + err.getvalue()


def issues_of(text: str) -> list[str]:
    try:
        bc.parse(text)
    except bc.ContractInvalid as exc:
        return exc.issues
    return []


def main() -> int:
    sample_text = SAMPLE.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        def write(name: str, text: str) -> str:
            path = tmp_dir / name
            path.write_bytes(text.encode("utf-8"))
            return str(path)

        # AC1 valid sample
        code, out = run_cli("contract", "validate", str(SAMPLE))
        check("AC1 valid sample: VALID, exit 0", code == 0 and out.strip() == "VALID", out)
        contract = bc.load(SAMPLE)
        check("sample has no open questions", not contract.open_questions)

        # AC2 approved without formula_intent
        approved = sample_text.replace("status: draft", "status: approved", 1)
        check("approved sample is valid", not issues_of(approved), str(issues_of(approved)))
        missing = "\n".join(
            line for line in approved.splitlines() if "formula_intent" not in line
        ) + "\n"
        code, out = run_cli("contract", "validate", write("missing.yaml", missing))
        check("AC2 missing formula_intent: INVALID, kpis.0.formula_intent", code == 1 and out.startswith("INVALID") and "kpis.0.formula_intent" in out, out)
        for field in ("grain", "currency"):
            lines = []
            in_kpi = False
            for line in approved.splitlines():
                if line.startswith("kpis:"):
                    in_kpi = True
                elif in_kpi and line and not line.startswith((" ", "-")):
                    in_kpi = False
                if in_kpi and line.strip().startswith(f"{field}:"):
                    continue
                lines.append(line)
            found = issues_of("\n".join(lines) + "\n")
            check(f"approved KPI without {field} is invalid", any(f"kpis.0.{field}" in i for i in found), str(found))
        draft_missing = missing.replace("status: approved", "status: draft", 1)
        check("a draft may lack formula_intent", not issues_of(draft_missing), str(issues_of(draft_missing)))

        # AC3 blocking question
        blocked = approved + (
            "open_questions_placeholder: x\n"
        )
        blocked = approved.replace("open_questions: []", "open_questions:\n- id: Q1\n  text: How is Revenue defined?\n  blocking: true\n  source: rule\n  status: open\n  answer: null")
        code, out = run_cli("contract", "validate", write("blocked.yaml", blocked))
        check("AC3 blocking question prevents approval, names Q1", code == 1 and "Q1" in out and "open_questions.0" in out, out)
        answered = blocked.replace("status: open", "status: answered").replace("answer: null", "answer: Net revenue")
        check("answered blocking question allows approval", not issues_of(answered), str(issues_of(answered)))
        non_blocking = blocked.replace("blocking: true", "blocking: false")
        check("non-blocking open question allows approval", not issues_of(non_blocking), str(issues_of(non_blocking)))
        draft_blocked = blocked.replace("status: approved", "status: draft", 1)
        check("a draft may have an open blocking question", not issues_of(draft_blocked), str(issues_of(draft_blocked)))

        # AC4 round trip
        out_path = tmp_dir / "roundtrip.yaml"
        digest = bc.save(bc.load(SAMPLE), out_path)
        check("AC4 round trip: identical bytes", out_path.read_bytes() == SAMPLE.read_bytes())
        check("save returns the sha256 of the file", digest == bc.content_hash(out_path.read_bytes()) and digest.startswith("sha256:"))
        check("saved file is LF-only", b"\r" not in out_path.read_bytes())
        digest2 = bc.save(bc.load(out_path), tmp_dir / "again.yaml")
        check("saving twice gives the same hash", digest == digest2)
        full = sample_text.replace("open_questions: []", "open_questions:\n- id: Q1\n  text: Wie ist Umsatz definiert? €\n  blocking: false\n  source: llm\n  status: answered\n  answer: Netto\nlocked_facts_x: 1").replace("locked_facts_x: 1\n", "")
        full = full.replace("locked_facts: []", "locked_facts:\n- id: F1\n  text: Net Revenue excludes cancelled orders\n  source_question_id: Q1\n  locked_at: '2026-10-16T09:00:00Z'")
        loaded = bc.parse(full)
        save_path = tmp_dir / "full.yaml"
        bc.save(loaded, save_path)
        check("non-ASCII text survives a round trip as UTF-8", bc.load(save_path) == loaded and "€" in save_path.read_text(encoding="utf-8"))
        check("saved keys follow the contract order", save_path.read_text(encoding="utf-8").index("run_id:") < save_path.read_text(encoding="utf-8").index("kpis:"))

        # AC5 schema
        code, out = run_cli("contract", "schema")
        schema = json.loads(out)
        check("AC5 schema prints valid JSON containing kpis", code == 0 and "kpis" in schema["properties"])
        schema_path = tmp_dir / "schema.json"
        code, _ = run_cli("contract", "schema", "--output", str(schema_path))
        raw = schema_path.read_bytes()
        check("--output writes UTF-8 JSON with LF", code == 0 and json.loads(raw) and b"\r" not in raw and not raw.startswith(b"\xff\xfe"))
        check("committed schemas/business_contract.schema.json is current", json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == bc.json_schema(), "regenerate with: python -m app.cli contract schema --output schemas/business_contract.schema.json")
        check("schema forbids unknown fields", schema.get("additionalProperties") is False)

        # Edge cases
        found = issues_of("kpis: [\n  - name: x\n")
        check("YAML syntax error reports line and column", len(found) == 1 and "line" in found[0] and "column" in found[0], str(found))
        code, out = run_cli("contract", "validate", write("empty.yaml", ""))
        check("empty file: INVALID, empty document", code == 1 and "INVALID" in out and "empty document" in out, out)
        check("comment-only file is an empty document", issues_of("# nothing\n") == ["empty document"])
        found = issues_of(sample_text.replace("\nkpis:", "\nkpi:", 1))
        check("typo field kpi: extra field not permitted", "kpi: extra field not permitted" in found, str(found))
        found = issues_of(sample_text.replace("rationale:", "rationalle:", 1))
        check("nested unknown field has its path", any(i == "kpis.0.rationalle: extra field not permitted" for i in found), str(found))
        dup = sample_text.replace("kpis:\n", "kpis:\n- name: net revenue\n", 1)
        found = issues_of(dup)
        check("duplicate KPI (case-insensitive) names both entries", any("kpis.1.name" in i and "kpis.0.name" in i for i in found), str(found))
        found = issues_of(sample_text.replace("schema_version: 1", "schema_version: 2"))
        check("newer schema_version: unsupported schema_version 2", found == ["unsupported schema_version 2"], str(found))
        found = issues_of("- a\n- b\n")
        check("a list document is invalid", len(found) == 1)
        q = "open_questions:\n- {id: Q1, text: a}\n- {id: Q1, text: b}"
        found = issues_of(sample_text.replace("open_questions: []", q))
        check("duplicate question ids are invalid", any("open_questions.1.id" in i for i in found), str(found))
        facts = "locked_facts:\n- {id: F1, text: a, locked_at: x}\n- {id: F1, text: b, locked_at: x}"
        found = issues_of(sample_text.replace("locked_facts: []", facts))
        check("duplicate fact ids are invalid", any("locked_facts.1.id" in i for i in found), str(found))
        found = issues_of(sample_text.replace("locked_facts: []", "locked_facts:\n- {id: F1, text: a, source_question_id: Q9, locked_at: x}"))
        check("fact pointing to a missing question is invalid", any("source_question_id" in i for i in found), str(found))
        found = issues_of(sample_text.replace("status: draft", "status: finished", 1))
        check("unknown status is invalid", any(i.startswith("status:") for i in found), str(found))
        found = issues_of(sample_text.replace("version: 1\nstatus", "version: 0\nstatus", 1))
        check("version below 1 is invalid", any(i.startswith("version:") for i in found), str(found))
        code, out = run_cli("contract", "validate", str(tmp_dir / "nope.yaml"))
        check("missing file: exit 2 with a message", code == 2 and "not found" in out.lower(), out)
        bad = tmp_dir / "bad.yaml"
        bad.write_bytes(b"\xff\xfe\x00bad")
        code, out = run_cli("contract", "validate", str(bad))
        check("non-UTF-8 file is INVALID, not a crash", code == 1 and "INVALID" in out, out)

        # Direct construction enforces the same rules
        try:
            bc.BusinessContract(run_id="r", business_objective="x", status="approved", kpis=[bc.Kpi(name="A")])
            direct = False
        except ValueError:
            direct = True
        check("constructing an approved contract with an incomplete KPI raises", direct)
        check("data_gap KPI status is accepted", not issues_of(sample_text.replace("status: draft", "status: data_gap", 2).replace("status: data_gap", "status: draft", 1)))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: " + "; ".join(failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
