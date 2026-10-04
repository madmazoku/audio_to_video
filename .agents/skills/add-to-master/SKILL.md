---
name: add-to-master
description: "Publish the current repository changes through the standard AI-numbered GitHub workflow: inspect and scope the diff, create an AI-N feature branch from master, commit and push it, open a pull request into master, merge it with GitHub CLI, preserve the remote branch, and synchronize local master. Use when the user asks to add, publish, merge, or deliver the current changes to master through a branch and PR."
---

# Add to Master

Publish only the explicitly reviewed task changes. Preserve unrelated working-tree changes throughout the workflow.

## Prepare

1. Confirm the repository root, remote, current branch, and working-tree state.
2. Inspect tracked and untracked changes. Identify the exact files belonging to the requested change; never stage unrelated files merely to obtain a clean tree.
3. Verify GitHub CLI authentication before creating a branch.
4. Fetch remote branch metadata.
5. Determine the next unused `AI-<n>` by scanning local branches, remote branches, and merged PR titles. Do not reuse an existing number.
6. Derive metadata from the current reviewed diff only. Do not reuse an earlier task, branch, or PR description.
7. Use different detail levels while keeping the same scope:
   - branch: `AI-<n>-<very-short-slug>` with only the few keywords needed to identify the change;
   - commit: `AI-<n>: <concise change summary>` as a short, informative description of what changed;
   - PR title: `AI-<n>: <concise change summary>` as a short, readable title, usually the same as or close to the commit subject;
   - PR body: the expanded description, including the motivation, material changes, verification performed, and relevant caveats.
8. Do not force the branch slug to repeat the full commit or PR title. Keep it substantially shorter.

Example:

```text
branch: AI-12-llama-planner
commit: AI-12: migrate planner to llama-cpp and remove legacy paths
PR title: AI-12: migrate planner to llama-cpp and remove legacy paths
PR body: detailed motivation, changed components, compatibility notes, and checks
```

## Create and publish the branch

1. Start from an up-to-date `master`. If switching branches would overwrite working-tree changes, stop and report the conflict; never stash, reset, or discard user changes without explicit approval.
2. Create `AI-<n>-<very-short-slug>` from `master`.
3. Stage only the reviewed task files using explicit paths. Avoid `git add -A` in a dirty worktree.
4. Review `git diff --cached --name-status`, `git diff --cached`, and `git diff --cached --check`.
5. Commit with the generated commit message.
6. Push the branch to `origin` and set its upstream.

## Create and merge the PR

1. Check whether a PR already exists for the branch. Reuse it instead of creating a duplicate.
2. Create a PR into `master` with `gh pr create` when needed.
3. Merge through GitHub only:

   ```text
   gh pr merge <PR_NUMBER> --merge
   ```

4. Do not use `git merge` locally and do not pass `--delete-branch`.
5. If repository settings automatically delete merged branches, report that the preservation requirement could not be guaranteed.

## Synchronize and verify

1. Switch to `master`.
2. Run `git pull origin master`.
3. Verify:
   - the PR is merged;
   - local `master` contains the GitHub merge commit;
   - the feature branch remains on `origin`;
   - unrelated working-tree changes remain untouched.
4. Report the branch, commit, PR number/link, merge result, final `master` commit, and any files intentionally left unstaged.

## Guardrails

- Never create a local merge commit for this workflow.
- Never delete the source branch.
- Never stage archives, generated media, logs, credentials, or unrelated files unless the user explicitly includes them.
- Never derive metadata from stale PR history when it conflicts with the current diff.
- Stop before any step that would require discarding or overwriting user work.
- If authentication, branch protection, checks, or merge conflicts block progress, preserve the branch and report the exact blocker.
