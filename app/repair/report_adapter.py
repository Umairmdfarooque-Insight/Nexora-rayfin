"""The report adapter of the repair service (T-603).

A report defect is repaired from the plan: the file of the failing visual (or page) is regenerated
from `ReportPlan.json` and the approved ModelSpec by the same generator that made the report, so
nothing but that file changes. The fix goes into a staging copy of the current version; the scope
guard compares it with the version in place and allows only the files of the failing objects. A kept
attempt becomes the next `report/v<n>`, is published to the sandbox and validated again (appended to
ValidationReport.json as a repair attempt). No model is involved: an object the plan itself can't
bind is not repaired, it is escalated to the user.
"""

import re
import shutil
from pathlib import Path
from typing import Any

from app.contracts.report_plan import ReportPlan
from app.contracts.validation_report import ValidationCheck, ValidationRun
from app.fsutil import replace_file
from app.report.pbir_generator import PbirGenerationError, build_files, file_bytes, folder_digest
from app.report.pbir_reader import PageRead, read_report
from app.report.validators import RENDERS, files_of
from app.repair.models import ClassifiedError
from app.repair.policy import RepairPolicy
from app.store.base import RunStore
from app.workflow.clarification import NotFound
from app.workflow.report_stage import PLAN_FILE, REPORT_DIR, _next_version
from app.workflow.report_validate_stage import ReportEnvironment, run_report_validation

ARTIFACT = "Report"
MODEL_ID = re.compile(r"semanticmodelid=([^\"\s;]+)")


def report_repair_code(check: ValidationCheck) -> str:
    """The policy key of a failed Gate G5 check."""
    if check.id == "G5.SCHEMA":
        return "REPORT_STRUCTURE" if check.object == "report" else "REPORT_SCHEMA"
    if check.id == "G5.BIND":
        return "REPORT_BINDING"
    if check.id == "G5.QUERY":
        return "REPORT_QUERY"
    return check.code or check.id


def read_tree(folder: Path) -> dict[str, bytes]:
    folder = Path(folder)
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.relative_to(folder).parts[0] != RENDERS
    }


def changed_files(previous: Path, new: Path) -> set[str]:
    a, b = read_tree(previous), read_tree(new)
    return {name for name in a.keys() | b.keys() if a.get(name) != b.get(name)}


class ReportRepairAdapter:
    artifact = ARTIFACT

    def __init__(self, env: ReportEnvironment, version: int, folder: Path, *, items_client: Any, pbi: Any):
        self.env, self.version, self.folder = env, version, Path(folder)
        self.deps = dict(items_client=items_client, pbi=pbi)
        self.touched: set[str] = set()
        self.notes: list[str] = []
        self.staging = Path(env.run_dir) / REPORT_DIR / ".repair.tmp"
        plan_path = Path(env.run_dir) / PLAN_FILE
        if not plan_path.is_file():
            raise NotFound("No report plan: a report is repaired from its plan")
        self.plan = ReportPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        self.pages: list[PageRead] = read_report(self.folder)

    # -- classification ----------------------------------------------------------------------

    def classify(self, errors: list[ValidationCheck], policy: RepairPolicy) -> list[ClassifiedError]:
        out = []
        for c in errors:
            if c.status != "fail":
                continue
            code = report_repair_code(c)
            out.append(ClassifiedError(c.fingerprint, c.object, c.id, code, policy.classify(code), c.message))
        return out

    # -- fixes ----------------------------------------------------------------------------------

    def begin_attempt(self) -> None:
        self.touched, self.notes = set(), []
        if self.staging.exists():
            shutil.rmtree(self.staging)

    def _model_id(self) -> str:
        found = MODEL_ID.search((self.folder / "definition.pbir").read_text(encoding="utf-8"))
        return found.group(1) if found else "model"

    def _stage(self) -> None:
        """A copy of the current version (without renders) to put the regenerated files in."""
        if self.staging.exists():
            return
        self.staging.mkdir(parents=True)
        shutil.copy2(self.folder / "definition.pbir", self.staging / "definition.pbir")
        shutil.copytree(self.folder / "definition", self.staging / "definition")

    def deterministic_fix(self, errors: list[ClassifiedError]) -> None:
        """Regenerate the file of each failing visual or page from the plan."""
        wanted = [e for e in errors if e.code in ("REPORT_BINDING", "REPORT_SCHEMA")]
        if not wanted:
            return
        try:
            files = build_files(self.plan, self._model_id(), self.env.spec)
        except PbirGenerationError as exc:
            self.notes.append(f"the plan itself can't be generated: {exc}")
            return
        for e in sorted(wanted, key=lambda e: e.object):
            self._write_object(e.object, files)

    @staticmethod
    def _rel_of(obj: str, files: dict) -> str | None:
        kind, _, name = obj.partition(":")
        if kind == "visual":
            return next((r for r in files if r.endswith(f"/visuals/{name}/visual.json")), None)
        if kind == "page":
            return f"definition/pages/{name}/page.json" if f"definition/pages/{name}/page.json" in files else None
        return None

    def _write_object(self, obj: str, files: dict) -> bool:
        rel = self._rel_of(obj, files)
        if rel is None:
            self.notes.append(f"{obj}: not in the report plan, so it can't be regenerated")
            return False
        self._stage()
        target = self.staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file_bytes(files[rel]))
        self.touched.add(obj)
        return True

    def regenerate(self, objects: list[str], prune_pages: list[str] | None = None) -> None:
        """Write the files of `objects` (`visual:<id>`, `page:<name>`) from the current plan into the
        staging copy, for a reviewer's regeneration. Visual folders of `prune_pages` that the plan no
        longer has are removed. Raises PbirGenerationError if the plan can't be generated."""
        files = build_files(self.plan, self._model_id(), self.env.spec)
        for obj in objects:
            self._write_object(obj, files)
        for page in prune_pages or []:
            self._stage()
            for folder in sorted((self.staging / "definition" / "pages" / page / "visuals").glob("*")):
                if f"definition/pages/{page}/visuals/{folder.name}/visual.json" not in files:
                    shutil.rmtree(folder)

    def llm_fix(self, llm: Any, errors: list[ClassifiedError]) -> None:
        for e in errors:
            self.notes.append(f"{e.object}: a report is repaired from its plan, not by a model")

    # -- the scope guard ------------------------------------------------------------------------

    def has_changes(self) -> bool:
        return self.staging.exists() and bool(changed_files(self.folder, self.staging))

    def scope_guard(self, errors: list[ClassifiedError]) -> list[str]:
        """The changed files outside the failing visuals and pages (and what a fix touched)."""
        allowed: set[str] = set()
        for obj in {e.object for e in errors} | self.touched:
            allowed |= files_of(obj, self.pages)
            kind, _, name = obj.partition(":")
            if kind == "visual":  # the file the plan puts it in, if the report has it elsewhere
                allowed |= {r for r in self._plan_files() if r.endswith(f"/visuals/{name}/visual.json")}
        return sorted(changed_files(self.folder, self.staging) - allowed)

    def _plan_files(self) -> set[str]:
        try:
            return set(build_files(self.plan, self._model_id(), self.env.spec))
        except PbirGenerationError:
            return set()

    def discard(self) -> None:
        if self.staging.exists():
            shutil.rmtree(self.staging)

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes

    # -- keeping and validating an attempt -------------------------------------------------------

    def validate(self, repair_attempt: int | None) -> ValidationRun:
        """Keep the staged copy as the next version, publish it and validate it."""
        root = Path(self.env.run_dir) / REPORT_DIR
        self.version = _next_version(root)
        final = root / f"v{self.version}"
        replace_file(self.staging, final)
        self.folder = final
        self.pages = read_report(final)
        return run_report_validation(
            self.env, final, self.version, folder_digest(final), repair_attempt=repair_attempt, **self.deps
        )
