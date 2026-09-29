# Project Guidelines & Agent Rules

## Quick Links

- **[PRODUCT.md](docs/PRODUCT.md)** — Product vision, target user, MVP scope, and non-goals
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — System architecture, data flow, component details, and data models
- **[AGENTS.md](AGENTS.md)** — Agent rules, execution protocol, and coding standards
- **[docs/TODO.md](docs/TODO.md)** — Backlog and task tracking

## Core Directives
- **Scope & Focus:** Keep tasks small and incremental. Never attempt to build or rewrite a massive feature in a single prompt.
- **Subagent Delegation:** Offload heavy codebase searching, file exploration, or multi-file analysis to subagents to prevent main thread context flooding.
- **Fail Loudly:** Do not implement silent fallbacks or hidden default values. Surface configuration errors explicitly.
- **Verification First:** Never mark a task complete when verification is failing. Never commit known-broken code.

## Pre-Task Git Hygiene

Before starting **any** new TODO task, the agent must follow this exact sequence:

```
CHECK STATUS → CHECK BRANCH → CHECKOUT MAIN → PULL LATEST → CREATE FEATURE BRANCH → BEGIN WORK
```

1. **CHECK STATUS:** Run `git status` and inspect the output for uncommitted changes.
2. **CHECK BRANCH:** Run `git branch --show-current` to record the current branch.
3. **CHECKOUT MAIN:** Run `git checkout main` to switch to main.
4. **PULL LATEST:** Run `git pull origin main` to fast-forward to the latest remote state.
5. **CREATE FEATURE BRANCH:** Run `git checkout -b feat/<short-description>` (or `fix/` / `chore/` as appropriate) from the updated main.
6. **BEGIN WORK:** Only now proceed to inspect and implement the TODO item.

**Uncommitted changes rule:** If `git status` shows any uncommitted or unstaged changes, **stop immediately**. Do **not** overwrite, discard, stash, or commit them. Report the situation to the user and wait for instructions before proceeding.

## Execution Protocol

Every task must follow this sequence:

```
INSPECT → PLAN → IMPLEMENT → VERIFY → REVIEW → FIX → RE-VERIFY → UPDATE STATE → COMMIT → PUSH → PR → STOP
```

1. **INSPECT:** Read relevant files before making assumptions. Use the Explorer subagent for codebase investigation.
2. **PLAN:** Decompose work into atomic, independently verifiable steps. Use the Planner subagent for complex features.
3. **IMPLEMENT:** Work on one atomic TODO item at a time. Do not silently expand a task.
4. **VERIFY:** Run tests, typecheck, lint, or manual validation. Confirm the change works locally.
5. **REVIEW:** Run the independent Reviewer subagent. The implementation agent must never approve its own work.
6. **FIX:** Resolve all BLOCKER and HIGH review findings.
7. **RE-VERIFY:** Re-run verification after fixes. A PR must not be opened while verification is failing.
8. **UPDATE STATE:** Update docs/TODO.md to reflect completion only after verification passes and reviewer approves.
9. **COMMIT:** Create an atomic commit with a conventional commit message.
10. **PUSH:** Push a dedicated feature branch (never work directly on main for feature work).
11. **PR:** Open a pull request representing the completed atomic unit.
12. **STOP:** After opening the PR, stop working. Do not automatically begin the next TODO item.

## Subagent Responsibilities

### Explorer (Read-Only)
- Architecture discovery and pattern analysis
- External API investigation when necessary
- **NO file modifications** — read-only operations only
- Use glob, grep, read tools extensively

### Planner (No Implementation)
- Decompose large features into independently implementable tasks
- Identify dependencies between tasks
- Produce task lists with clear acceptance criteria
- **NO implementation** — planning only

### Reviewer (Read-Only)
- Independent pre-merge code reviewer for atomic changes
- Reviews work produced by another agent
- **MUST NOT** modify production files, tests, documentation, docs/TODO.md, or configuration
- Determines whether changes are safe, correct, sufficiently tested, architecturally appropriate, and ready to merge
- Returns verdict: APPROVE or CHANGES_REQUESTED
- **NO file modifications** — review only

#### Reviewer Output Format

Every review must clearly separate findings into four categories:

1. **BLOCKING** (BLOCKER / HIGH): Issues that must be fixed before the PR can proceed. These are correctness bugs, security issues, data-loss risks, or violations of acceptance criteria.
2. **NON-BLOCKING** (MEDIUM / LOW): Observations that do not prevent merge but are worth noting. These may include style improvements, pre-existing gaps, or optional enhancements.
3. **SUGGESTED FOLLOW-UP**: Potential future work identified during review that is outside the scope of the current task. These are not actionable in the current PR.
4. **OUT-OF-SCOPE**: Observations that are informational only (e.g., tooling limitations, future strictness plans) and should not be turned into tasks.

The reviewer must not bundle categories. Each finding must be placed in exactly one category.

### Tester (Verification Focus)
- Run pytest and analyze test outputs
- Investigate test failures and suggest fixes
- Track test coverage and pass/fail status
- **NO production implementation** unless explicitly instructed

## Branching

Each meaningful atomic TODO task should be implemented on its own feature branch.

Branch names should follow a consistent convention:

- `feat/<short-description>` for new features
- `fix/<short-description>` for bug fixes
- `chore/<short-description>` for maintenance tasks

**Agents must never work directly on `main`.** Always create a feature branch first. Before committing, verify you are on a feature branch (`git branch --show-current`). The repository's local Git hooks provide two layers of enforcement:

- **`.githooks/pre-commit`** — rejects any commit made directly on `main`.
- **`.githooks/pre-push`** — rejects any push originating from `main`.

## Parallel Worktree Workflow

Independent atomic TODO tasks may run in separate Git worktrees via the `@ykaratkou/opencode-worktree` plugin. Worktrees are created in `../ai-agent-worktrees/` (configured in `.opencode/worktree.jsonc`).

### Rules

1. **Isolation:** Each agent must operate only within its assigned worktree. Never read from, write to, or reference files outside the worktree directory.
2. **No cross-worktree dependencies:** Agents must never rely on or modify uncommitted changes from another worktree. Each worktree is a self-contained, committed snapshot.
3. **Independence check:** Only genuinely independent tasks should be run in parallel. If two tasks share files or have sequential dependencies, run them serially on the same branch.
4. **Verify before editing:** Before making changes, agents must confirm they are in the correct worktree (`pwd` should resolve to a path under `../ai-agent-worktrees/`) and on the correct feature branch (`git branch --show-current`).
5. **docs/TODO.md minimization:** When multiple agents may be running in parallel, modify `docs/TODO.md` minimally to reduce merge conflicts. Update only the specific item being completed, and avoid reformatting or restructuring the file.
6. **Existing workflow preserved:** The existing branch → implement → verify → review → commit → push → PR workflow remains unchanged within each worktree.

### Worktree Lifecycle

```
git worktree add ../ai-agent-worktrees/<branch-name> -b <branch-name>
# ... work inside the worktree ...
# ... commit and push from within the worktree ...
git worktree remove ../ai-agent-worktrees/<branch-name>
```

Or use the plugin tools: `worktree_create` (creates worktree + branch + copies config) and `worktree_delete` (commits, removes worktree, cleans up).

## Pull Requests

A pull request represents a completed atomic engineering unit.

One meaningful TODO item should normally correspond to one PR. Do not combine unrelated TODO items into one PR.

A PR should include:

- a stakeholder-facing "What this changes for users" section at the top
- a concise description of what changed
- why the change exists
- the TODO item being completed
- acceptance criteria
- verification performed
- any known limitations

**Stakeholder section:** Every PR must begin with a short plain-English section placed directly above the existing `## Summary` section:

```markdown
## What this changes for users

<2–4 sentences in plain English explaining:
- what problem the user/stakeholder experiences today
- what changes after this PR
- why the change matters
>
```

Writing rules:

- Write for someone with no knowledge of the codebase.
- Avoid file names, function names, classes, internal state names, API names, and implementation details.
- Explain the behavior or experience, not the code.
- Keep it concise: usually 2–4 sentences.
- State the user-visible problem first, then the improvement.
- If there is no direct end-user impact, explain the stakeholder/operational value instead, such as reliability, safety, cost control, maintainability, or reduced risk.
- Do not exaggerate impact or claim benefits the PR does not actually provide.
- Keep the existing technical `## Summary`, `## Why`, acceptance criteria, verification, known limitations, and other PR sections unchanged.

Example:

```markdown
## What this changes for users

Before this change, a caller who said "no," gave an uncertain answer, or did not respond to the final summary could leave the conversation in an unclear state. After this change, the agent only completes the request after the caller clearly confirms the location, vehicle, and issue, and it asks for corrections when needed. This reduces the chance of submitting incorrect roadside-assistance details.
```

**Long PR messages:** When the PR body exceeds a single line, pipe a heredoc into `gh pr create --body-file -`. Avoid passing long multi-line strings directly to `--body` arguments, as shell quoting of special characters (backticks, parentheses, URLs) frequently causes parsing errors. Example:

```bash
cat <<'EOF' | gh pr create --fill --body-file -
## What this changes for users

Plain-English stakeholder summary goes here.

## Summary

Describe the change here.

## Verification

- All tests pass
EOF
```

## Review Rules

Every meaningful PR must receive an independent review from the Reviewer subagent before it is considered ready.

The implementation agent must never approve its own work.

The reviewer must not modify files or commit changes.

### Handling Review Findings

**Blocking findings (BLOCKER / HIGH):**
- Must be fixed before the PR can proceed.
- The implementation agent must address every BLOCKER and HIGH finding.
- Re-verification is required after fixes.

**Non-blocking findings (MEDIUM / LOW):**
- Do not block the current task unless they are necessary for correctness.
- The implementation agent must explicitly review and acknowledge all non-blocking findings before proceeding after an APPROVE.
- The implementation agent must not expand the current task solely to address non-blocking findings unless they are necessary for correctness.
- Do not address non-blocking findings in the current PR unless the finding is necessary to satisfy the current task's acceptance criteria or prevent a correctness/security issue. Otherwise, leave the current scope unchanged and create a follow-up TODO when appropriate.

**Suggested follow-up work:**
- Legitimate follow-up work identified by the reviewer should be added to `docs/TODO.md` as a new TODO item.
- Do not silently ignore suggested follow-up work.
- Do not turn out-of-scope, optional, or already-acceptable observations into TODO items.

**Out-of-scope observations:**
- Observations that are informational only (e.g., tooling limitations, future strictness plans) should not be turned into TODO items.
- The reviewer remains read-only and must never modify `docs/TODO.md`.

### After Reviewer APPROVE

When the reviewer returns **APPROVE**, the implementation agent must process every NON-BLOCKING and SUGGESTED FOLLOW-UP finding before proceeding to commit/PR.

#### Per-finding decision

For each finding, the agent must explicitly decide one of:

1. **Address now** — Only if the finding is necessary for correctness or to satisfy the current task's acceptance criteria. Do not expand scope merely to address style or preference.
2. **Create follow-up TODO** — If the finding represents legitimate future engineering or product work that is outside the current scope.
3. **No action** — If the finding is merely informational, already acceptable, already covered by existing work, or truly out of scope.

#### Creating follow-up TODOs

If a finding is classified as legitimate follow-up work, the agent **must** add a corresponding item to `docs/TODO.md` before completing the current task. Do not merely mention that the finding is being "tracked" in the response.

Every new follow-up TODO must:

- Be concrete and independently verifiable
- Preserve the intent of the reviewer finding
- Include acceptance criteria
- Include verification criteria
- Avoid copying the reviewer's prose verbatim

The agent must not add duplicate TODO items. Before adding one, check whether an equivalent task already exists in `docs/TODO.md`.

The agent must not expand the current PR solely to resolve non-blocking findings.

#### Completion gate

Before proceeding from reviewer APPROVE to commit/PR, verify:

- All BLOCKER/HIGH findings are resolved
- All non-blocking findings were explicitly classified (address now / follow-up TODO / no action)
- All legitimate follow-up work has been written to `docs/TODO.md`
- No informational/out-of-scope findings were incorrectly added to `docs/TODO.md`

#### Final task summary

The task summary (reported to the user after PR creation) must include:

- Reviewer verdict
- Blocking findings (count, all resolved)
- Non-blocking findings (count, each classified with rationale)
- Follow-up TODOs created (list each item added to `docs/TODO.md`)
- Findings intentionally not actioned (list each with brief reason)

## Verification Requirements

A PR must not be opened while the relevant verification is failing due to the current change.

At minimum, run the repository's configured:

- tests
- linting
- type checking
- build checks where applicable

## TODO State Management

Do not mark a TODO task complete merely because implementation is finished.

Mark it complete only after:

- implementation is complete
- verification passes
- reviewer approves the change
- PR has been created successfully

If the repository policy requires merge before completion, document that distinction explicitly in docs/TODO.md rather than falsely marking the task complete.

## Merge Policy

The implementation agent should NOT automatically merge its own PR unless explicitly instructed by the user or repository policy.

Creating the PR and merging the PR are separate operations.

## Stop Condition

After successfully opening the PR, stop working.

Do not automatically begin the next TODO item. The next task begins only after the user starts another task/session or explicitly instructs the agent to continue.

## Code Standards
- **File Length:** Keep files clean and modular. If a file grows too large, propose refactoring.
- **No Unnecessary Drift:** When editing existing code, respect existing styles and variable naming conventions. Do not alter formatting outside the scope of your task.
- **Prefer Existing Patterns:** Use existing project patterns over introducing new abstractions.
- **Validate Boundaries:** Validate external data at system boundaries.
- **Never Invent:** Never invent APIs, schemas, requirements, or external behavior.

## Terminal & Pathing Rules
- Always use relative paths (e.g., `folder/` or `./folder/`) for all file system operations and bash commands.
- NEVER use absolute paths starting with a forward slash (e.g., `/folder/`) to prevent attempting to write to the OS root directory.

## Python Environment Rules
- Never use system-wide pip installs.
- Always create a local virtual environment (e.g., `python3 -m venv .venv`) before installing any dependencies.
- Always activate the virtual environment (`source .venv/bin/activate`) before running `pip install` commands, generating a `requirements.txt`, or executing Python scripts.
- Ensure the `.venv` directory is added to `.gitignore`.

## Git Hygiene Rules
- Always initialize a git repository if one does not exist.
- Ensure `.gitignore` is configured to exclude `.venv/`, `__pycache__/`, `.env`, and large data files.
- **MANDATORY:** Create a separate atomic git commit immediately after successfully completing *each* individual functional step or sub-component. Never bundle multiple distinct tasks into a single commit.
- Never commit secrets, API keys, or large data files.
- **Inspect before committing:** Run `git diff` to verify changes before committing.
- **Conventional commits:** Use conventional commit messages (feat:, fix:, docs:, test:, chore:).

## Verification & Checkpoints
- **Frequent Commits:** Run or recommend `git commit` checkpoints after every successful sub-component or passing test.
- **Verification:** Always verify that code compiles, passes tests, or runs cleanly before yielding control back to the user.
- **Test Commands:** Run `source .venv/bin/activate && python -m pytest tests/ -v` to execute tests.
- **Typecheck:** Run `source .venv/bin/activate && python -m mypy .` if mypy is configured.
- **Lint:** Run `source .venv/bin/activate && python -m ruff check .` if ruff is configured.

## Current Tooling Status

| Tool | Status | Command |
|------|--------|---------|
| pytest | Not installed | `pip install pytest` then `python -m pytest tests/ -v` |
| mypy | Not installed | `pip install mypy` then `python -m mypy .` |
| ruff | Not installed | `pip install ruff` then `python -m ruff check .` |
