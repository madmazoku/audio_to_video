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

1. Validate repository root.
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
12. Create a PR into `master` using the generated title and body.
13. Merge the PR with a merge commit.
14. Do not delete the branch during merge.

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
