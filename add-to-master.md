# AI PR Workflow Skill

This repository defines a reusable AI skill for creating a feature branch, committing current changes, pushing the branch to `origin`, opening a GitHub PR into `master`, and merging that PR while preserving the branch.

## Skill purpose

Use this skill when you want to add a change to `master` with the standard AI branch workflow:

1. create or switch to a new branch
2. commit all current changes with a descriptive `AI-<n>: ...` message
3. push the branch to `origin`
4. create a PR into `master`
5. merge the PR using a merge commit
6. keep the branch available on `origin`

## Workflow

This skill generates all branch and PR metadata automatically from the current changes.

- infer a short descriptive summary from the current diff content;
- determine the next unused `AI-<n>` number by scanning existing branch names and merged PR titles for `AI-<n>`;
- construct branch name as `AI-<n>-<short-description>`;
- construct commit message as `AI-<n>: <short description>`;
- construct PR title and body from the same generated summary.

## Workflow steps

1. Validate repository root and ensure on `master` branch.
2. Run `git status --short` and ensure the working tree is ready.
3. Summarize the current diff into a concise phrase describing the change.
4. Determine the next unused AI number `n` by inspecting existing branches and merged PRs using `AI-<n>` naming.
5. Construct branch name:
   - `AI-<n>-<short-description>`
6. Construct commit message:
   - `AI-<n>: <short description>`
7. Construct PR body describing the fix from the diff.
8. Create or switch to the generated branch.
9. Stage all changes:
   - `git add -A`
10. Commit with the generated message.
11. Push the generated branch to `origin`.
12. Create a PR into `master` using the generated title and body via `gh pr create`.
13. **Merge the PR using `gh pr merge --merge`** (merge commit strategy, no branch deletion flag).
14. **Wait for GitHub to confirm the merge and update `master`**.
15. **Do NOT use local `git merge` commands.**
16. The branch is preserved on `origin` by default (no `--delete-branch` flag).

## Critical implementation notes

- Always use `gh pr merge <pr_number> --merge` without `--delete-branch`.
- Never use `git merge` locally after pushing to origin; PR merging via GitHub CLI ensures clean history and integration with branch protection rules.
- After `gh pr merge` completes, the local `master` may lag behind; use `git pull origin master` to sync if needed.
- The branch name remains available on `origin` for future work or reference.

## Usage examples

### GitHub Copilot

Prompt Copilot with a concise request referencing this repo skill, for example:

```
Use the AI PR workflow skill in this repo to generate a new AI branch and commit based on the current edit diff. Create a new branch named AI-<n>-<summary>, commit with message AI-<n>: <summary>, push it, open a PR into master with a generated body, and merge it without deleting the branch.
```

### Codex / generic assistant

Use a similar instruction in the prompt:

```
In this repository, use the saved AI PR workflow skill to create a branch, commit the current changes, push, create a PR into master, and merge. Generate branch and commit names automatically from the current diff and assign a new AI-<n> number.
```

## Notes

- `AI-<n>` is a repository-specific incrementing change ID, analogous to an issue number.
- The skill should scan existing branch names and merged PR titles for used `AI-<n>` values.
- `short-description` should be slugified from the change summary.
- The branch must remain on `origin` after merge.
- This skill is intended as a reusable pattern for future AI-enabled changes.

## Common errors and how to avoid them

### Error: Dupe merge commits in git history

**Symptom:** git log shows both `Merge pull request #N from...` and `Merge AI-<n>...` for the same branch.

**Cause:** Using local `git merge` after `gh pr merge` has already completed. This creates two separate merge commits.

**Fix:** Never use `git merge` locally once the branch is pushed to origin. Always use `gh pr merge <pr_number> --merge` via GitHub CLI. After merge, sync with `git pull origin master` if needed.

### Error: PR not created

**Symptom:** `gh pr create` command fails or reports "already exists".

**Cause:** PR may have been created manually on GitHub, or the remote branch name differs from what the skill expects.

**Fix:** Always verify the exact branch name matches before running the skill. Use `git branch --show-current` to confirm.

### Error: Branch is deleted after merge

**Symptom:** Branch no longer exists on `origin` after PR merge.

**Cause:** The `--delete-branch` flag was used in `gh pr merge`, or GitHub repository settings auto-delete branches.

**Fix:** Use `gh pr merge <pr_number> --merge` **without** `--delete-branch`. Ensure GitHub repository settings do not have auto-delete enabled.
