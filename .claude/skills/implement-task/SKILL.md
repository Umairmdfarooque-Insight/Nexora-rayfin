---
name: implement-task
description: Implement the next task from the Nexora task plan in .claude/tasks/. Accepts a milestone file (e.g. M0-baseline.md or M0), a task ID (e.g. T-101), or nothing (next pending task in the whole plan). Implements exactly one task, then hands over the task's acceptance criteria for manual testing.
argument-hint: "[M0-baseline.md | M0 | T-101 | done T-101]"
---

# Implement a Nexora task

Argument: `$ARGUMENTS`

The plan is in `.claude/tasks/`. `README.md` holds the task index, the status column and the defaults assumed for the spec's open decisions. Each `M*-*.md` file holds the tasks of one milestone. Every task has a **Depends on · Spec** line and six sections: **Problem statement**, **Functional requirements**, **API contracts**, **Constraints**, **Edge cases and error handling** and **Acceptance criteria**. `CONTRACTS.md` defines the shared shapes (run, states and transitions, error envelope, approvals, gate results, validation reports) that tasks reference.

Status values used in the README index: `Not started` → `In progress` → `Awaiting manual test` → `Done`, plus `Needs Fabric re-test` (see step 1a).

**Hosting:** Nexora is being redeveloped to run entirely on Microsoft Fabric. Read `.claude/technical-documentation/fabric-hosting.md` and `.claude/tasks/CONTRACTS.md#fabric-operations` before any task. Each task's **Depends on** line has a **Fabric hosting** part (`udf`, `job`, `ui`, or library), and a reworked task has a **Fabric rework** or **Fabric re-test** note under it. That note wins over older wording in the task's sections.

## 1. Resolve which task to work on

| Argument | Task |
|---|---|
| A milestone file or prefix (`M0-baseline.md`, `M0`, `m3`) | The first task in that file whose status is not `Done` |
| A task ID (`T-101`, `t101`) | That task |
| Empty | The first task in the README index whose status is not `Done` (a `Needs Fabric re-test` task counts as not done) |
| `done T-xxx` | Don't implement anything. The user confirms the acceptance criteria passed: set that task to `Done`, then report the next task in sequence. |

If the resolved task is `Awaiting manual test`, don't re-implement it. Remind the user of its acceptance criteria and ask whether they passed.

### 1a. A `Needs Fabric re-test` task

Its business logic was built and tested in the first build. Don't re-implement it. Instead:
1. Check its dependencies as in step 2, and create a branch as in step 3 (slug `fabric-retest-<short title>`).
2. Check that each of the task's operations is in the dispatcher (`app/fabric/dispatch.py`) with the host that CONTRACTS.md#fabric-operations gives it. Check the paths, bodies and status codes against the task, and every point of its **Fabric re-test** note: config read from `Files/config/`, files through the artifact store, tokens from the T-002 provider, no OBO left.
3. Fix only what those checks find. Anything bigger is a finding to report, not something to build.
4. Run the offline checks (step 5). Then set the status to `Awaiting manual test` and hand over (step 6), with the acceptance criteria written for Fabric: which ones run from Rayfin, the `nexora_api` test pane or a stage job, with the exact `call(...)` arguments or job parameters.

If the argument matches nothing, list the milestone files and stop.

## 2. Check before writing code

1. Read `.claude/CLAUDE.md`, `.claude/tasks/README.md` and the full task section.
2. **Dependencies:** every task in **Depends on** must be `Done`. If any isn't, stop and tell the user which ones block it, and offer to implement the first blocking task instead. Don't skip ahead silently, because each task's acceptance criteria assume the earlier tasks passed.
3. **Open decisions:** if this task appears under "First needed at" in the README's working-assumptions table, ask the user (AskUserQuestion) to confirm the default or choose another option before implementing. Record the answer in that table.
4. Read the spec sections the task cites (`.claude/spec/`, `.claude/technical-documentation/`), for example FR-012 or §12 G4. The spec's business rules and the LLM/deterministic split apply to every task.
5. Read the existing code the task touches. Extend and reuse it, and match its style. Don't write a parallel version.

## 3. Create and switch to the task branch

All work for the task happens on its own branch. Do this before changing any file. Skip this step for `done T-xxx`.

1. **Resuming:** if the task is `In progress` and the README index's **Branch** column names a branch, run `git switch <that branch>` and go to step 4. Don't create a new branch.
2. **Clean tree:** run `git status --short`. If there are uncommitted changes, stop and ask the user whether to commit them first (`/commit-push`) or carry them onto the new branch. Never stash or discard them yourself.
3. **Base branch:**
   - Default: `main`. Run `git fetch origin`, `git switch main`, then `git pull --ff-only`.
   - If a dependency task's branch (from the **Branch** column) is not merged into `main`, check with `git branch -r --merged origin/main`. The new branch must contain that work, so tell the user, and base on the dependency's branch instead. If the recorded branch no longer exists locally or on the remote, `/cleanup-branch` removed it after it was merged, so base on `main`.
4. **Name:** `task/<task-id>-<slug>`.
   - The slug is the task title in lowercase, kebab-case, at most 5 words, using `[a-z0-9-]` only.
   - Example: `task/T-101-run-model-state-machine`.
5. **Check the name is available**, both locally and on the remote:
   ```
   git show-ref --verify --quiet refs/heads/<name>      # exit 0 = exists locally
   git ls-remote --exit-code --heads origin <name>      # exit 0 = exists on remote
   ```
   If either one exists, append `-0` and check again, then `-1`, `-2` and so on until a free name is found. For example `task/T-101-run-model-state-machine-0`.
6. **Create it:** `git switch -c <name>`. Don't push it yet; `/commit-push` does that.
7. **Record it:** write the branch name in the task's **Branch** column in `.claude/tasks/README.md`. Tell the user which branch you are on.

Every later step for this task (implementing, verifying, status updates) stays on this branch. Never switch away mid-task, and never commit task work to `main`.

## 4. Implement

- Set the task's status to `In progress` in the README index.
- Implement every **Functional requirement**, exactly as the **API contracts** specify: endpoint paths, request and response shapes, CLI names, file layouts and config files. Use the shared shapes and error codes from `CONTRACTS.md`. If the implementation has to change a shared shape, update `CONTRACTS.md` on the same branch and say so in the handover.
- Respect every **Constraint**, and handle every row of **Edge cases and error handling** with the stated behavior and error code.
- Build nothing from later tasks. If something from a later task seems necessary, mention it instead of building it.
- Keep these spec rules:
  - LLM output is a candidate. Deterministic code validates and controls state.
  - No silent KPI assumptions.
  - Return a Data Gap instead of fabricating a calculation.
  - Never send raw rows to the LLM.
- Keep these Fabric rules (see `fabric-hosting.md`):
  - UDF and job handlers hold no business logic; they call the dispatcher, which calls the stage services.
  - Model calls, stage graphs and every access to a user's Lakehouse, SQL endpoint, Fabric REST or Power BI run only in jobs, as the user (T-002 token provider). A UDF touches only Nexora's control items.
  - Settings go through `app/config.py`: process environment, then the Variable library `nexora_settings`, then `.env` locally. Secrets come only from Key Vault (or `.env` locally).
  - Run files go through the `ArtifactStore` and manifests through the `RunStore`; never write a run file with `open()` directly.
  - A platform limit the task depends on (see the table in `fabric-hosting.md`) is checked against current Fabric documentation and recorded there. Don't assume it.
- Never read or print the values in `.env`, the Variable library or Key Vault. If a task needs new settings, add the names (with empty values) to `.env.example` and to the `nexora_settings` definition under `fabric/`.
- Never deploy to a non-sandbox workspace, and never write to production data. Nexora's own items go only to a `Nexora-*` home workspace (T-005).

## 5. Verify what you can

- Run everything that doesn't need a live Azure or Fabric sign-in: imports, schema validation, CLI commands, deterministic generators, offline unit checks, the dispatcher with the local stores (`RUN_STORE=file`, `ARTIFACT_STORE=local`), `python -m app.tools.package`, and `npm test` / `npm run build` in `rayfin-workload/`.
- The manual-test steps that need live Azure, Fabric or a browser sign-in are the user's to run. Don't claim they passed. If you did run one, report its real output.
- Fix anything that fails before handing over.

## 6. Hand over

- Set the task's status to `Awaiting manual test`.
- If commands, setup or architecture changed, update `.claude/CLAUDE.md`. For a reworked task, update its section there to describe the Fabric-hosted code, and remove the task from the pending list in "Target hosting". Record any platform limit you checked in `fabric-hosting.md`.
- Add or refresh the task's section in `.claude/MANUAL_TESTING.md` with its manual-test steps (exact commands and **Expected** results), following "Adding a task" in that file. Any script the manual test needs goes in `app/manual_tests/` and is committed, never left as a scratch or root-level file.
- Don't commit or push unless the user asks.
- Reply with:
  1. **What was built:** the branch name, files changed, and any decision made.
  2. **Verified by Claude:** what you ran, with the results.
  3. **Acceptance criteria to test manually:** each criterion, rewritten with the exact commands, URLs and payloads of the real implementation, with its **Expected** result. Mark which ones you already verified offline.
  4. **Next:** "When it passes, run `/implement-task done T-xxx`, then `/commit-push` to push this branch." Name the task that comes after it.
