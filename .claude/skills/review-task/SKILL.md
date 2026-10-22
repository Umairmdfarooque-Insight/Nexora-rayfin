---
name: review-task
description: Review the code of a Nexora task branch (for use in a T-xxx_test session). Checks the changes against the task's functional requirements, API contracts, constraints, edge cases and acceptance criteria, the spec rules, correctness, security and codebase conventions, then reports verified findings and a fix list to paste into the development session. Review only - does not modify code.
argument-hint: "[T-101]  (default: task from the current branch name)"
---

# Review a Nexora task

Argument: `$ARGUMENTS`

This review runs in a **separate session** from the one that wrote the code, so it isn't anchored on the developer's reasoning. Judge the code as it is. Don't assume the implementation is right because it exists.

**Review only.** Don't edit, create or delete project files, and don't commit, switch branches or stash. Fixes are made in the development session (`T-xxx`) using the fix list from step 6. If the user explicitly asks you to fix something here, remind them of the workflow first, and only then proceed.

## 1. Identify the task and the changes

- **Task ID:** from the argument, or else from the current branch (`git branch --show-current`, format `task/T-xxx-…`). If neither gives a task ID, ask which task to review.
- If the current branch is `main`, stop. Task work lives on task branches.
- Read:
  - `.claude/CLAUDE.md`
  - the task's full section in `.claude/tasks/M*-*.md`: the Depends on · Spec line, Problem statement, Functional requirements, API contracts, Constraints, Edge cases and error handling, and Acceptance criteria
  - `.claude/tasks/CONTRACTS.md`, for the shared shapes and error codes the task references
  - the spec sections the task cites, in `.claude/spec/` and `.claude/technical-documentation/`
  - `.claude/technical-documentation/fabric-hosting.md` and `.claude/tasks/CONTRACTS.md#fabric-operations`. The task's **Fabric hosting** line and its **Fabric rework** or **Fabric re-test** note win over older wording in its sections.
- For a `Needs Fabric re-test` task, the review is about the wiring: each operation is in the dispatcher with the right host and unchanged contract, and every point of the re-test note holds. The first build's logic isn't re-reviewed unless the branch changed it.
- Collect **all** changes on the branch, committed and uncommitted:
  ```
  git fetch origin
  git diff --stat $(git merge-base origin/main HEAD)
  git diff $(git merge-base origin/main HEAD)
  git status --short -uall            # untracked files are part of the change too
  ```
- Read each changed or new file **in full**, not just the hunks, plus the existing code it calls or replaces.

## 2. Check the task conformance

Build one checklist covering the task's sections. Give each row a status and evidence:

| Status | Meaning |
|---|---|
| ✅ Done | Implemented as specified, with `file:line` evidence |
| ⚠️ Partial | Some of it is missing or deviates. Say exactly what. |
| ❌ Missing | Not implemented |

| Task section | One checklist row per… | What to verify |
|---|---|---|
| Functional requirements | numbered requirement | The behavior exists and does what the requirement says |
| API contracts | endpoint, CLI command, function, file format and config file | Paths, methods, parameter names, request and response shapes, status codes, field names and file layouts match exactly. Shared shapes match `CONTRACTS.md`. Any deviation is a finding. |
| Constraints | constraint | The constraint holds in the code, for example read-only SQL, no LLM call, deterministic output or sandbox-only |
| Edge cases and error handling | table row | The case is handled with the stated behavior **and** the stated error code and HTTP status. A case that crashes or returns a different code is a finding. |
| Acceptance criteria | criterion | The steps can run as written: the commands, endpoints, paths and CLI names exist. The code can produce the **Expected** result. |

Also check:
- **Scope creep:** code that belongs to a later task. Name the task, for example "this is T-302 work".
- **Contract drift:** if `CONTRACTS.md` changed on the branch, check that every earlier task that uses the changed shape still works.

## 3. Review the code

| Area | What to look for |
|---|---|
| Spec rules | LLM output treated as a candidate, with deterministic code validating and controlling state. No silent assumptions for KPI definitions. A Data Gap returned instead of a fabricated calculation. No raw rows sent to the LLM (NFR-008). Generation and deployment only to the sandbox. The workflow state machine not bypassed. Stage graphs follow `CONTRACTS.md#stage-graphs`: no `RunStore` transitions or manifest writes inside a graph, every loop bounded by an attempt counter, routing only on validator output, no `interrupt()` or persistent checkpointer, LLM calls only through `app/llm/client.py`, LangSmith tracing off. |
| Correctness | Logic errors, wrong edge-case handling (empty, None, duplicates), state transitions that skip `RunStore`, errors swallowed silently, results that are non-deterministic where NFR-009 requires repeatability |
| Security | Secrets in code, logs, notebook output, the Variable library, `fabric/` or committed files. `.env` values read or printed. SQL built by string concatenation with untrusted input, or identifiers not quoted. Missing identity or run-authorization checks in the dispatcher (after T-105 and T-705). A token passed through a request body or a job parameter. |
| Fabric hosting | Business logic in a UDF or job handler instead of the dispatcher and services. A model call, stage graph, or access to a user's Lakehouse, SQL endpoint, Fabric REST or Power BI from a UDF. An OBO or `InteractiveBrowserCredential` path used in Fabric. A run file written outside the `ArtifactStore`, or a manifest outside the `RunStore`. Append-only files rewritten without the run lease. A deployment of Nexora items outside a `Nexora-*` home workspace. An operation whose host differs from CONTRACTS.md#fabric-operations without CONTRACTS.md being updated. A platform limit assumed but not recorded in `fabric-hosting.md`. |
| Conventions | Absolute imports (`from app.…`). Settings read through `app/config.py`. Existing modules and connectors reused instead of duplicated. Naming and comment density matching the surrounding code. New dependencies added to `requirements.txt`, so they reach the wheel. New settings added to `.env.example` with empty values and to the `nexora_settings` definition. |
| Hygiene | Leftover debug prints, dead code, hardcoded `saleslt` names or IDs where the task asked for them to be generalized, or files that shouldn't be committed (`runs/`, `.venv/`, `__pycache__/`) |

## 4. Verify before reporting

- **Prove each finding** by reading the exact code path. Where possible, also run an offline check that shows the problem.
- Drop anything you can't verify, or label it **Plausible** with the reason.
- Run the checks that don't need Azure, Fabric or a browser sign-in, and report their real output:
  - `python -m compileall -q app`
  - importing every changed module
  - `--help` for any new CLI command
  - any deterministic generator or validator the task added, run on sample input
  - the dispatcher for the task's operations with the local stores (`RUN_STORE=file`, `ARTIFACT_STORE=local`)
  - `python -m app.tools.package`, and `npm test` in `rayfin-workload/` if the UI changed
- Never claim a live Azure or Fabric step passed unless you ran it and saw the result.

## 5. Severity

- **Blocker:** an acceptance criterion can't pass, an API contract is broken, a constraint or spec rule is violated, or there's a security problem.
- **Major:** a bug in a realistic scenario, a functional requirement that is Partial or Missing, or an edge case that is unhandled or returns the wrong error code.
- **Minor:** a convention break, readability issue or small cleanup.

## 6. Report

Reply in this structure:

1. **Verdict:** ✅ **Ready for manual test**, or ❌ **Needs changes**. Any Blocker or Major finding means Needs changes. Add one line on why.
2. **Scope reviewed:** the branch, the base commit, and the number of files changed.
3. **Task checklist:** the table from step 2, grouped by task section.
4. **Findings**, most severe first. Each has:
   - Severity, then `file:line`, then a one-line title
   - **What happens:** concrete input or state, and the wrong result or crash
   - **Suggested fix:** one or two sentences, without writing the patch
   - Verified, or Plausible
5. **Checks run:** each command with pass or fail and the key output.
6. **Fix list for the dev session:** one fenced block the user can paste straight into the `T-xxx` session:
   ```
   Review of T-xxx found these issues. Fix them on the current branch, then hand back the manual test:
   1. [Blocker] app/...py:42 — ...
   2. [Major] ...
   ```
   Leave this out if there are no findings.
7. **Next:**
   - If Ready, offer to walk the user through the task's acceptance criteria one at a time, checking each result against its **Expected** value.
   - If Needs changes, tell the user to paste the fix list into the `T-xxx` session, and then run `/review-task` again, preferably in a fresh `_test` session.
