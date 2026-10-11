---
description: Commit, push, create PR, merge, and clean up after code changes are complete
allowed-tools: Read, Bash, mcp__github__create_pull_request, mcp__github__merge_pull_request, mcp__github__delete_branch
---

## Repository
All GitHub MCP calls in this command target:

```text
owner: basil-faruq
repo:  Powerlifting_Prilepins_Calender
url:   https://github.com/basil-faruq/Powerlifting_Prilepins_Calender.git
base branch: main
```

Pass `owner` and `repo` explicitly on every MCP call. Never infer them from the working directory.

## Scope
This command ships completed code changes from the Powerlifting Block Planner project root:

```text
Powerlifting Blocks/
```

The edit/debug iteration should already be complete before `/ship-feature` is invoked.
Use the actual Git diff in this directory as the source of truth for the commit message and pull request description.

**Note on the path:** the directory name contains a space. Every Git pathspec referring to it must be quoted (`"Powerlifting Blocks/"`), or Git will read it as two separate paths and silently match nothing.

## Step 0 — Verify remote and resolve scope

### 0a. Confirm the remote
```bash
git remote get-url origin
```

Accept either form:
- `https://github.com/basil-faruq/Powerlifting_Prilepins_Calender.git`
- `git@github.com:basil-faruq/Powerlifting_Prilepins_Calender.git`

A trailing `.git` may be absent; that is fine.

If `origin` points anywhere else, stop and report:

"origin points at <actual-url>, not basil-faruq/Powerlifting_Prilepins_Calender. Refusing to push."

If `origin` does not exist, stop and report the fix:

```bash
git remote add origin https://github.com/basil-faruq/Powerlifting_Prilepins_Calender.git
```

### 0b. Confirm the base branch exists
```bash
git rev-parse --verify main
```

If this fails, the repository's default branch is not `main`. Stop and report:

"No `main` branch found. This command assumes `main` is the default branch."

### 0c. Resolve the scope pathspec
```bash
git rev-parse --show-toplevel
```

Then check whether the project directory sits inside the repository root:

```bash
test -d "$(git rev-parse --show-toplevel)/Powerlifting Blocks" && echo "nested" || echo "root"
```

- `nested` → set `SCOPE` to `"Powerlifting Blocks/"`
- `root` → the repository root **is** the project root; set `SCOPE` to `.`

Use `SCOPE` in every pathspec below, always quoted.

## Step 1 — Identify current branch
```bash
git branch --show-current
```
Store this as CURRENT_BRANCH.

If CURRENT_BRANCH is `main`, stop. Never commit directly to main.

## Step 2 — Inspect completed changes
Run:
```bash
git status --short -- "$SCOPE"
git diff --staged -- "$SCOPE"
git diff -- "$SCOPE"
git log main..HEAD --oneline
```

If there are no staged or unstaged changes under the scope, stop and report:

"No changes found in Powerlifting Blocks/. Nothing to ship."

Use the changed files and diff to understand what was added, fixed, removed, or updated.

Generate a Conventional Commit message:
- feat: new feature
- fix: bug fix
- chore: config or tooling
- docs: documentation only
- refactor: internal code change without new behavior or a bug fix
- test: test-only changes

Rules:
- Lowercase
- No period at the end
- Under 72 characters
- Describe the user-visible or project-level outcome where possible
- Base the message only on the actual changes being shipped

Good: "feat: offer three reschedule options when a session is missed"
Good: "fix: floor working weights to the athlete's plate increment"
Bad: "feat: updated files"

## Step 3 — Stage and commit
Stage only changes from the project directory:

```bash
git add -- "$SCOPE"
git diff --staged -- "$SCOPE"
git commit -m "<generated-message>"
```

Report: "✓ Committed — <message>"

## Step 4 — Push to feature branch
```bash
git push -u origin CURRENT_BRANCH
```

Report: "✓ Pushed — CURRENT_BRANCH"

## Step 5 — Create PR via GitHub MCP
Use the GitHub MCP server to create a pull request:

```text
owner: basil-faruq
repo:  Powerlifting_Prilepins_Calender
head:  CURRENT_BRANCH
base:  main
```

Generate the PR title from the completed changes.

Title rules:
- Plain English
- No Conventional Commit prefix
- Concise description of the feature, fix, or change

Example: "Add reschedule options for missed sessions"

Build the PR description from the actual committed diff and changed files:

```markdown
## What this PR does
<one concise paragraph describing the completed change and its purpose>

## Changes
- <changed file or logical change with a one-line description>
- <changed file or logical change with a one-line description>

## Validation
- <tests, checks, or verification performed during the edit iteration>
- <any relevant manual verification>

## How to test
1. <command to start the affected part, e.g. `make infra && make api`, `make up`, or `make web`>
2. <steps needed to exercise the changed behavior>
3. <expected result>
```

Validation notes should reference what was actually run during the edit iteration — typically `make test`, `make lint`, and for anything touching weight calculation, the golden test that reproduces all 77 weights from `B1_Mine.xlsx`.

Do not reference `.claude/specs/`, `docs/specs/`, or any numbered spec file. Derive all PR content from the code changes, Git history, and verification already performed during the completed edit iteration.

Report: "✓ PR created — <PR URL>"

## Step 6 — Merge PR via GitHub MCP
Use the GitHub MCP server to merge the pull request just created.

```text
owner: basil-faruq
repo:  Powerlifting_Prilepins_Calender
merge_method: squash
```

Report: "✓ PR merged to main"

## Step 7 — Delete remote branch via GitHub MCP
Use the GitHub MCP server to delete CURRENT_BRANCH from GitHub after the merge.

```text
owner: basil-faruq
repo:  Powerlifting_Prilepins_Calender
branch: CURRENT_BRANCH
```

Report: "✓ Remote branch deleted"

## Step 8 — Switch to main and pull
```bash
git checkout main
git pull origin main
```

Report: "✓ Switched to main — up to date"

## Step 9 — Delete local feature branch
```bash
git branch -D CURRENT_BRANCH
```

Report: "✓ Local branch deleted"

## Final summary
Print:

```text
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
/ship-feature complete
✓ Committed — <message>
✓ Pushed — <branch>
✓ PR created and merged
✓ Remote branch deleted
✓ Switched to main
✓ Local branch deleted
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
```

## Rules
- Only push to `basil-faruq/Powerlifting_Prilepins_Calender`; verify `origin` in Step 0 before any push
- Only ship changes under `Powerlifting Blocks/`
- Always quote the scope pathspec — the directory name contains a space and an unquoted pathspec matches nothing
- Pass `owner` and `repo` explicitly on every GitHub MCP call
- This command assumes the default branch is `main`
- Never commit directly to main
- Never commit `.env`; if it appears in `git status`, stop and report that it is untracked and must stay that way
- Never invent change details that are not supported by the Git diff or completed verification
- Always use squash merge
- Always delete both remote and local feature branches after merge
- If GitHub MCP is not connected, stop and say:
  "GitHub MCP is not connected. Run /mcp to check connection."
- If push fails due to no upstream, use `git push -u origin CURRENT_BRANCH`
- Never proceed to merge if PR creation fails
- Never delete the branch if the merge fails
