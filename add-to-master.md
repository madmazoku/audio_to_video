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

- infer a short descriptive summary from the current diff content only, not from previous branch names, past PR titles, or earlier feature ideas;
- determine the next unused `AI-<n>` number by scanning existing branch names and merged PR titles for `AI-<n>`;
- construct branch name as `AI-<n>-<short-description>`;
- construct commit message as `AI-<n>: <short description>`;
- construct PR title and body from the same generated summary.

## Workflow steps

**STEPS 1-7: Prepare metadata**
1. Validate repository root and ensure on `master` branch. If you are currently on an existing `AI-<n>` branch or a merge commit branch, switch back to `master` before creating a new branch. Do not start a new AI branch from an already merged AI branch or from a PR merge commit branch.
2. Run `git status --short` and inspect both tracked and untracked changes. Explicitly identify the files that belong to the requested change. Preserve unrelated user changes and never include them merely because they are present in the working tree.
3. Summarize the current diff into a concise phrase describing the change. Use the exact changed files and current task, not the previous branch purpose or an earlier feature idea.
4. Confirm the summary by reading the diff of the changed files. If the summary does not match the actual changes, rewrite it until it clearly describes the current work.
5. Fetch current remote branch metadata, then determine the next unused AI number `n` by inspecting local/remote branches and merged PRs using `AI-<n>` naming. Verify GitHub CLI authentication before creating the branch so a known authentication failure is found before repository mutations.
6. Construct branch name from the actual change summary: `AI-<n>-<short-description>`.
7. Construct commit message from the actual change summary: `AI-<n>: <short description>`.
8. Construct PR title and body from the same generated summary.
9. Do not use task labels, previous branch names, or old feature scopes to name the branch, commit, or PR.

**STEPS 8-11: Create and push branch**
8. Create or switch to the generated branch: `git checkout -b AI-<n>-<short-description>`
9. Stage only the explicitly reviewed files that belong to the change, using `git add <file>...`. Run `git diff --cached --name-status` and `git diff --cached --check` before committing. Do not use `git add -A` in a dirty worktree unless every tracked and untracked path was explicitly confirmed as in scope.
10. Commit with the generated message: `git commit -m "AI-<n>: ..."`
11. Push the generated branch to `origin`: `git push origin AI-<n>-<short-description>`

**STEPS 12-17: Create PR and merge via GitHub CLI ONLY**
12. Create a PR into `master` using: `gh pr create --head AI-<n>-<short-description> --base master --title "AI-<n>: ..." --body "..."`
13. Note the PR number from the output (e.g., PR #7).
14. **Merge using GitHub CLI ONLY**: `gh pr merge <PR_NUMBER> --merge` (merge commit, no `--delete-branch`).
15. Switch back to `master`: `git checkout master`
16. Sync local `master`: `git pull origin master`
17. Verify the merge completed and local `master` is current: `git status --short --branch` and `git log --oneline --decorate -1` should show the merge commit on `master`.
18. **The branch is automatically preserved on `origin` after merge** (no deletion flag was used).

**CRITICAL: DO NOT create local merge commits.** After step 11 (`git push`), proceed directly to step 12-14 using `gh pr` commands. Never use `git merge` locally.

**ADDITIONAL SAFETY RULES**
- If the current branch name already contains `AI-<n>`, do not reuse that branch name for a new change.
- Never stash, reset, discard, or stage unrelated user changes just to make the working tree clean. A feature branch may be created with unrelated unstaged files present as long as the commit scope is explicit and verified.
- Always start a new AI branch from `master`; do not start from `AI-6`, `AI-7`, or any other existing AI branch.
- If you have task-related unstaged changes after a failed workflow attempt, preserve them, return to `master` only when that checkout is safe, and resume with explicit file staging. Do not stash or reset unrelated user work without approval.
- If a PR already exists for the generated branch name, do not create a duplicate PR; instead use the existing PR number.
- If a merge commit is present in the current working branch (`Merge pull request #...`), return to `master` and create a clean branch from there.
- Generate the branch name, commit message, and PR metadata from the current diff only. Do not reuse titles, labels, or feature areas from previous work.
- Do not label a new branch with a previous feature area (for example, `-video-config-validation`) unless the current diff actually changes that area.
- If the branch summary is not clearly tied to the changed files, pause and rewrite the branch name until it matches the current work.
- After the PR is merged, always checkout `master` and confirm the merge with `git pull origin master` before continuing.

## Critical implementation notes

**The ONLY correct workflow after `git push`:**
```
git push origin AI-<n>-<short-description>     # Step 11: Push branch
gh pr create ... --head AI-<n>-... --base master   # Step 12: Create PR (get PR number)
gh pr merge <PR_NUMBER> --merge                # Step 14: Merge via GitHub CLI ONLY
git pull origin master                         # Step 15: Sync local
```

**Do NOT:**
- ❌ Use `git merge` locally after pushing (creates duplicate merge commits)
- ❌ Use `git merge` instead of `gh pr merge` (bypasses branch protection and creates wrong commit messages)
- ❌ Skip the PR creation step (violates repository standards)
- ❌ Use `--delete-branch` flag with `gh pr merge` (deletes the source branch, we want to preserve it)
- ❌ Copy a branch name, commit message, or PR title from a previous AI branch or PR if it is unrelated to the current diff.
- ❌ Name the new branch after a past task area unless the current changed files actually belong to that area.
- ❌ Assume the current commit is about the previous task; always inspect the actual changed files before naming.
- ❌ Let old PR metadata or old commit history determine the new branch name; current diff content must be the source of truth.

**Why this matters:**
- `gh pr merge` creates a GitHub merge commit with metadata and shows the PR in history
- Local `git merge` creates a separate, redundant merge commit
- Using both creates a forked merge history that's hard to untangle
- Always let GitHub handle the merge via `gh pr merge`

**After merge completes:**
- GitHub automatically updates the PR to "Merged"
- Local `master` may lag; use `git pull origin master` to sync
- The AI-<n> branch remains on `origin` for reference/debugging (not deleted)

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
