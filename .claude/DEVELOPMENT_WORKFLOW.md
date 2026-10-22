# Nexora — Development Workflow with Claude Code

This guide explains how to build Nexora one task at a time with Claude Code. It covers how to implement each task, review and test it in a separate session, and get it into `main`.

The first build of every task (T-001 to T-706) is done and runs locally. Nexora is now being **redeveloped to run entirely on Microsoft Fabric** ([fabric-hosting.md](technical-documentation/fabric-hosting.md)). [`tasks/README.md`](tasks/README.md) shows what each task needs:

| Status | Meaning | What you do |
|---|---|---|
| `Not started` | Rewritten for Fabric; the code must change | The full cycle below |
| `Needs Fabric re-test` | Logic unchanged; only its entry point moves to a User Data Function or a job | `/implement-task T-xxx` checks the wiring and hands over the acceptance criteria. Run them on Fabric, then continue from Step 5 |
| `Done` | Not affected (T-004, T-201) | Nothing |

Work starts again with **T-001**, in order. A `Needs Fabric re-test` task can only be tested once T-102 (operations) is done, and in Rayfin once T-104 is done.

---

## The cycle at a glance

```
 ┌────────────────────────── one task, e.g. T-001 ──────────────────────────┐
 │                                                                          │
 │  1. Read the task once, and edit it if needed (.claude/tasks/M0-*.md)    │
 │                     │                                                    │
 │  2. Dev session "T-001":  /implement-task T-001                          │
 │        → creates branch task/T-001-…  → implements the changes there     │
 │                     │                                                    │
 │  3. Test session "T-001_test": /review-task  + run the manual test       │
 │                     │                                                    │
 │         ┌── result not as expected? ──┐                                  │
 │         │  undo / extra prompt / fix  │ ← back to the dev session        │
 │         └─────────────────────────────┘                                  │
 │                     │ passes                                             │
 │  4. /implement-task done T-001                                           │
 │  5. /commit-push            → pushes the task branch to GitHub           │
 │  6. GitHub portal: create PR → resolve conflicts → merge into main       │
 │  7. /cleanup-branch         → confirm → pull main → delete branch        │
 │                                                                          │
 └──────────────────────────────── next task: T-002 ────────────────────────┘
```

---

## Before you start (once)

1. Clone or open the repository folder:
   ```
   cd "C:\Users\umdfaroo\Downloads\NEXORA-main\NEXORA-(Rayfin)"
   ```
2. Create your environment file:
   - Copy `.env.example` to `.env` and fill in the values.
   - `.env` is gitignored. Never commit it.
3. Set up Python:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Make sure you can sign in to Azure / Microsoft Fabric (a browser sign-in) and push to the GitHub repo.
5. **Fabric (from T-005 on):**
   - A Fabric capacity you may use, and permission to create the workspace `Nexora-dev` on it.
   - Run `python -m app.tools.package`, then `python -m app.tools.deploy_home --env dev --create Nexora-dev` (T-005). This creates the home workspace items: `nexora_api`, `nexora_stage_runner`, `nexora_env`, `nexora_control_db`, `nexora_control`, `nexora_settings` and `nexora_telemetry`.
   - Fill in the `nexora_settings` Variable library values for `dev` (never secrets), and give your account read access to the Key Vault secret `azure-openai-api-key` (T-705).
   - After a code change that a Fabric test needs, run `package` and `deploy_home --env dev` again, so that the Environment holds the new wheel.
   - **Rayfin (from T-104 on):** a tenant admin enables Fabric workloads in developer mode for you. Run the workload's dev server in `rayfin-workload/` and open Rayfin in the Fabric portal. The exact steps are recorded by T-104 in [fabric-hosting.md](technical-documentation/fabric-hosting.md).
6. **Always start Claude Code from this folder**, so it loads `.claude/CLAUDE.md` and the skills:
   ```
   claude
   ```

### Skills used in this workflow

| Command | What it does |
|---|---|
| `/implement-task <task ID or milestone file>` | Creates a branch for the task, then implements it on that branch only. Ends with the manual test steps. |
| `/review-task [task ID]` | Run in the `_test` session. Reviews the task branch against the task, the spec rules and the code conventions, then gives a verdict and a fix list for the dev session. It doesn't change code. |
| `/implement-task done <task ID>` | Marks the task `Done` after you've validated it. |
| `/commit-push` | Commits all changes with a generated description and pushes the current branch to GitHub. |
| `/cleanup-branch` | After the PR is merged: asks for confirmation, pulls the latest `main` and deletes the feature branch. |

---

## Sessions: one for development, one for testing

Each task uses **two separate Claude Code sessions**, named after the task:

| Session | Name | Purpose |
|---|---|---|
| Development | `T-001` | Implements the task: writes and changes code |
| Review / test | `T-001_test` | Reviews the code from the development session and helps run the manual test. It does **not** write the feature. |

A fresh session for review means the reviewer doesn't share the developer's assumptions. It reads the code as it actually is.

**Create and name a session:**
1. Open a new terminal in the repo folder and run `claude`.
2. Type `/rename T-001`, or `/rename T-001_test` for the test session.

**Return to a session later:** run `claude --resume` (or `/resume` inside Claude) and pick the session by name.

> **Both sessions use the same folder, so they are on the same branch.** The test session sees the dev session's changes immediately.
> - Don't let both sessions edit code at the same time.
> - Don't switch branches in the test session.

---

## Step-by-step for each task

### Step 1 — Read the task once, and adjust it if needed

1. Open the task in its milestone file. For T-001, that's [`tasks/M0-baseline.md`](tasks/M0-baseline.md).
2. Read the six sections: **Problem statement**, **Functional requirements**, **API contracts**, **Constraints**, **Edge cases and error handling** and **Acceptance criteria**. Also check the **Depends on** line. Shared shapes the task references are in [`tasks/CONTRACTS.md`](tasks/CONTRACTS.md).
3. If something is wrong, unclear, or conflicts with what you need, **edit the task file now**, before implementing. The skill implements exactly what the task says.

### Step 2 — Implement in the development session

1. Start a new session and run `/rename T-001`.
2. Run:
   ```
   /implement-task T-001
   ```
   You can also pass the milestone file, `/implement-task M0-baseline.md`, which picks the next unfinished task in it.
3. The skill does the following:
   - **Checks first:** the task's dependencies are `Done`, and your working tree is clean.
   - **Asks you to confirm** any open design decision the task relies on.
   - **Creates the branch** `task/T-001-<short-title>`. If that name already exists locally or on GitHub, it adds `-0`, `-1` and so on until the name is free. It then switches to the branch, and **all changes for this task are made on it only**.
   - **Implements** the task and runs the checks it can run without Azure.
   - **Sets** the task to `Awaiting manual test` and gives you the manual test steps, with exact commands.

### Step 3 — Review and test in the test session

1. Open another terminal in the same folder, run `claude`, then `/rename T-001_test`.
2. Run the review:
   ```
   /review-task T-001
   ```
   With no argument, it reads the task ID from the branch name. It reports:
   - **a verdict:** ✅ Ready for manual test, or ❌ Needs changes
   - **a task checklist:** each functional requirement, API contract, constraint, edge case and acceptance criterion as done, partial or missing, with file and line evidence
   - **verified findings**, by severity: Blocker, Major or Minor
   - **the offline checks it ran**, with their results
   - **a ready-to-paste fix list** for the dev session
3. If the verdict is **Ready**, ask the test session to walk you through the acceptance criteria: "walk me through the acceptance criteria of T-001 one at a time".
4. **You run the manual test of each acceptance criterion.** Any step that needs Azure sign-in, Fabric or a browser has to be checked by you, and compared against its **Expected** result.
   - **On Fabric:** deploy the branch's code to `Nexora-dev` first (`package`, then `deploy_home --env dev`).
   - A `udf` operation is tested from Rayfin, or from the `nexora_api` test pane.
   - A `job` operation is tested from Rayfin, or by reserving it and running `nexora_stage_runner` from the portal. Follow its progress in the Monitoring hub.
   - Run files are in `nexora_control` → `Files/runs/<id>/`, or locally with `python -m app.cli runs pull <id>`.

### Step 4 — If the results are not as expected

Go back to the **development session (`T-001`)** and use one of these options:

| Situation | What to do |
|---|---|
| The change went in a wrong direction | **Undo it.** Ask in the chat ("undo the last change to X"), or use `/rewind` to roll back to an earlier point in the conversation and its file changes. |
| The approach is right but something is missing or wrong | **Add a prompt** in the chat describing the problem. Paste the **fix list** from `/review-task`, or the failing test output from the test session. |
| The **task itself** asks for something conflicting or wrong | **Fix the task first.** Remove or correct the conflicting part in the task file. Then set the task's status in [`tasks/README.md`](tasks/README.md) back to `In progress` and run `/implement-task T-001` again. It resumes on the **same branch**. |

Then repeat Step 3 in the test session. Use a **new** `_test` session if the old one is full of outdated findings.

### Step 5 — Mark the task done

When every **acceptance criterion** passes, run this in the development session:
```
/implement-task done T-001
```
This marks T-001 `Done` in `tasks/README.md`, on the task branch.

### Step 6 — Commit and push the branch

```
/commit-push
```
- **Safety checks:** it blocks secrets and `.env`, asks about binary or large files, and keeps local folders out of the commit.
- **Description:** it writes the commit description from what was changed, including which task was implemented and its status.
- **Push:** it pushes the **task branch** to GitHub and gives you a link to create a pull request.

### Step 7 — Create the pull request and merge it (GitHub portal, done by you)

1. Open the PR link from `/commit-push`, or go to the repo, then **Pull requests → New pull request**.
2. Set **base: `main`** ← **compare: `task/T-001-…`**, then **Create pull request**.
3. **If GitHub reports conflicts**, resolve them with one of these:
   - **Simple conflicts:** use **Resolve conflicts** in the portal, edit the files, then **Mark as resolved → Commit merge**.
   - **Bigger conflicts:** resolve them locally. In the dev session, ask: "merge origin/main into this branch and help me resolve the conflicts". Then run `/commit-push` again.
4. Review the PR, then **Merge pull request**. The remote `main` now has the task.

### Step 8 — Clean up the branch

In the development session, run:
```
/cleanup-branch
```
- It shows a **caution message** and asks you to confirm that:
  - a PR was created and **merged** into `main`
  - the remote feature branch was **synced** with `main`

  It also shows what it found: whether the branch is merged, has unpushed commits, or is behind `main`.
- You choose: **delete the local and remote branch**, **delete the local branch only**, or **cancel**. If you cancel, create or merge the PR first.
- After you confirm, it switches to `main`, **pulls the latest changes from the remote `main`** into your local `main`, and deletes the feature branch.

### Step 9 — Next task

You're back on an up-to-date `main`. Start the next task, T-002, with **new** sessions named `T-002` and `T-002_test`, and repeat from Step 1.

---

## Per-task checklist

```
[ ] Read the task in .claude/tasks/<milestone>.md (and its Fabric hosting line); edit it if needed
[ ] New session → /rename T-xxx → /implement-task T-xxx
[ ] Confirm the branch task/T-xxx-… was created
[ ] Deploy the branch to Nexora-dev (package + deploy_home --env dev) if the test runs on Fabric
[ ] New session → /rename T-xxx_test → /review-task → run the manual test
[ ] Fix in the T-xxx session if needed (undo / extra prompt / fix the task) → retest
[ ] /implement-task done T-xxx
[ ] /commit-push
[ ] GitHub: create PR → resolve conflicts → merge into main
[ ] /cleanup-branch → confirm → main updated, branch deleted
```

## Rules to keep in mind

- **One task per branch, one branch per task.** Never commit task work directly to `main`.
- **Follow the order.** A task can start only when the tasks it depends on are `Done`. The skill enforces this.
- **The repo is public.** Don't paste secrets into prompts, task files or code. Keep `.env` local.
- **Only you mark a task done**, after the manual test passes. Claude reports what it verified, but it can't sign in to Azure or Fabric for you.
- **Merging is always done by you** through a GitHub PR. The skills never push to `main` for task work, and never force-push.
- **Fabric targets:** deploy Nexora's own items only into a `Nexora-*` home workspace. Models, reports and bridge notebooks go only to the sandbox, and only the promotion job (T-702) writes to a target workspace.
