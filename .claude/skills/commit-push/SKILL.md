---
name: commit-push
description: Commit the working-tree changes and push them to the remote branch, with a commit message generated from what changed since the last commit (including which .claude/tasks were implemented). Optional argument - a branch name to commit on, or a short note to include in the message.
argument-hint: "[branch-name | \"note for the message\"]"
---

# Commit and push

Argument: `$ARGUMENTS`

Running this skill is the user's go-ahead to commit and push. Only the safety checks below can stop it. The repo is **public** on GitHub, so anything pushed is published.

## 1. Inspect

Run these together:
- `git status --short`
- `git diff HEAD --stat`
- `git diff HEAD`, the full diff. Read it, because the message comes from it.
- `git log -5 --oneline`
- `git branch --show-current`
- `git remote -v`
- `git rev-parse --abbrev-ref @{upstream}`, which may fail if the branch has no upstream.

If nothing changed and no local commits are unpushed, say so and stop.

## 2. Choose the branch

- If the argument looks like a branch name (no spaces), switch to it with `git switch <name>`, or create it with `git switch -c <name>` if it doesn't exist. The uncommitted changes carry over.
- Otherwise, use the current branch.
- If the argument is a quoted note, keep it for the commit message.

## 3. Safety checks

Stop and ask the user if any of these fail. Never bypass them.

1. **Secrets:** `.env`, or any file with credentials, must not be staged. Check the full diff for keys, tokens, passwords, connection strings, client secrets and long random-looking strings that are newly added. Exception: `.env.example` with **empty** values is fine.
2. **Large or binary files:** list any added file over 1 MB, or any binary such as `.docx`, `.pdf`, `.zip` or `.pbix`, and ask whether to include it. The spec `.docx` contains internal design, so it needs explicit confirmation before it goes into the public repo.
3. **Generated or local files:** `.venv/`, `__pycache__/`, `runs/` and `node_modules/` must not be staged. If any show up, add them to `.gitignore` instead of committing them.

## 4. Write the commit message

Base it on the diff, not on assumptions.

```
<Imperative summary, max ~70 chars, e.g. "Implement T-101 run store and state machine">

<1–2 sentences: why / what this change achieves.>

Tasks:
- T-101 Run model, state machine and run store — Awaiting manual test
  (only if .claude/tasks/README.md statuses changed; list each changed task with its new status)

Changes:
- <grouped by area: app/…, rayfin-ui/…, .claude/…, config — one line each, what changed>

Notes:
- <optional: decisions taken, follow-ups, the user's note from the argument>

<attribution trailer>
```

- **Tasks section:** compare `.claude/tasks/README.md` against `HEAD` to see which statuses changed. If none changed, leave the section out.
- **Attribution trailer:** end the message with the commit attribution line from the current system instructions, if one is given.
- Use a heredoc so the multi-line message survives the shell.

## 5. Commit and push

1. Stage the files by path with `git add <paths>`. Use `git add -A` only once the safety checks have passed and nothing unwanted is listed.
2. `git commit -F - <<'EOF' … EOF`. If a hook fails, fix the problem and make a **new** commit. Never use `--no-verify` and never amend.
3. Push:
   - If an upstream exists, `git push`.
   - Otherwise, `git push -u origin <branch>`.
   - If the push is rejected as non-fast-forward, run `git pull --rebase` and inspect the result. If it's clean, push again. If it conflicts, stop and show the conflicts.
   - **Never** force-push.
4. Report:
   - the branch
   - the commit hash and summary line
   - the files committed (count, with the notable ones)
   - the push result
   - the GitHub URL of the branch: `https://github.com/<owner>/<repo>/tree/<branch>`
   - a pull-request link, if the branch isn't `main`
