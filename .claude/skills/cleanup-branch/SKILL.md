---
name: cleanup-branch
description: After a feature/task branch is merged, switch to main, pull the latest remote main into local main, and delete the feature branch (locally, and optionally on GitHub). Always shows a caution message and asks for confirmation that a PR was created and the branch is synced with main before deleting anything.
argument-hint: "[branch-name]  (default: current branch)"
---

# Clean up a feature branch

Argument: `$ARGUMENTS`

Deleting a branch is hard to undo, and deleting it on GitHub affects anyone else using it. **Never delete anything before the user confirms in step 3.**

## 1. Identify the branch

- **Target:** the branch named in the argument, or else the current branch (`git branch --show-current`).
- If the target is `main`, or is empty because HEAD is detached, stop: "cleanup-branch only deletes feature branches."
- Check the branch exists locally (`git show-ref --verify refs/heads/<branch>`) and on the remote (`git ls-remote --heads origin <branch>`). Note which of the two exist.

## 2. Gather the facts

Run `git fetch origin --prune` first, then run these together:

| Check | Command | Meaning |
|---|---|---|
| Uncommitted changes | `git status --short` (on the target branch) | If anything is listed, **stop**. Ask the user to run `/commit-push` or discard the changes themselves. |
| Unpushed commits | `git log origin/<branch>..<branch> --oneline` | Local work that isn't on GitHub yet |
| Remote behind local | Same as above. If there's no `origin/<branch>`, the branch was never pushed | |
| Merged into main | `git merge-base --is-ancestor <branch> origin/main` (exit 0 = merged) | A regular merge |
| Squash or rebase merged | `git diff origin/main...<branch> --quiet` succeeds, or `git cherry origin/main <branch>` shows only `-` lines | The content is already in main |
| Branch behind main | `git rev-list --count <branch>..origin/main` | How far main has moved since the branch was synced |

Work out the owner and repo from `git remote get-url origin`. You'll need them for these links:
- Create a PR: `https://github.com/<owner>/<repo>/compare/main...<branch>?expand=1`
- Check PRs: `https://github.com/<owner>/<repo>/pulls?q=head:<branch>`

## 3. Caution message and confirmation

Show this message, filled in with the facts from step 2:

> ⚠️ **Caution: about to delete branch `<branch>`**
>
> Before deleting, make sure:
> 1. **A pull request was created** for `<branch>` into `main` and has been **merged**. Create one: <compare link> · Check: <PR link>
> 2. **The remote branch is synced with main.** It's `<n>` commits behind `main`. If the PR isn't merged yet, sync it with `git merge origin/main` on the branch, then push, so the PR reflects the current main.
>
> Status found:
> - Merged into `origin/main`: **yes / no / content already in main (squash)**
> - Unpushed local commits: **<count, or none>**
> - Exists on GitHub: **yes / no**
>
> ❗ If the branch is **not merged**, its commits will only survive on GitHub (if it was pushed). Deleting both copies can lose work permanently.

Then ask with AskUserQuestion:
- **Delete local and remote branch:** only recommended when the branch is merged.
- **Delete local branch only:** keeps the GitHub copy.
- **Cancel:** I'll create or merge the PR first.

Put the recommended option first: "Delete local and remote" if the branch is merged, "Cancel" if it isn't.

If the branch is **not merged** and the user still chooses to delete it, ask a **second** explicit confirmation that names the number of commits that will be lost. Do this before using `-D`.

On **Cancel**, stop. Remind the user of the PR link and of `git merge origin/main` to sync the branch.

## 4. Update main and delete

1. `git switch main`
2. `git pull --ff-only origin main`. If this fails because local `main` has diverged, stop and show the user. Never reset or force anything.
3. Delete the local branch:
   - Merged: `git branch -d <branch>`.
   - Squash-merged or confirmed twice: `git branch -D <branch>`, because `-d` refuses a squash-merged branch.
4. Only if the user chose remote deletion: `git push origin --delete <branch>`.
5. `git fetch origin --prune`
6. In `.claude/tasks/README.md`, leave the task's **Branch** column unchanged as history. `/implement-task` treats a branch that no longer exists as merged.

## 5. Report

- `main` is now at `<hash> <subject>`, and you pulled `<n>` new commits.
- Deleted: the local branch and/or the remote branch, or what was kept.
- Next: `/implement-task` to start the next task. It will branch from the updated `main`.
