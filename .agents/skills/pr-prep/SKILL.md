---
name: pr-prep
description: Final review of the current branch before a PR, then put the PR up; run it before opening or proposing any PR. A short numbered checklist the agent works top to bottom and ticks off with evidence (bugs and architecture reviewed, bad comments removed, duplicate code merged, dead code and files removed, docs updated, CI's checks passing, PR up). Use when the user asks to review, clean up or prep a branch for a PR, or to "put up a PR" after feature work.
---

# PR prep

Work the steps in order. Scope is the current branch against `origin/main`,
committed and uncommitted. `/pr-prep [title]` gives the PR title.

## How to run it

- Copy the checklist into your task list before you start, one task per step.
- Tick a step only after you have seen its evidence: a command's output, a
  grep, a finding fixed. Reasoning is not evidence.
- A step you cannot finish stays unticked and goes in the report with why.
  Never tick it or skip it quietly.
- No size exemption. A one-line diff runs every step.
- Keep behavior and the look as the user last approved them. Touch only what
  the branch added, changed or made dead.
- Never bare `git stash`. Never deploy. Push and open a PR only when this
  request asks for it. A refused tool call is a stop.

## Checklist

1. **Current with main.** `git fetch origin main`, then rebase on
   `origin/main` or merge it in. On a conflict keep main's change and re-apply
   the branch's intent.
   Evidence: `git merge-base --is-ancestor origin/main HEAD` succeeds.

2. **Bugs and architecture.** Read the AGENTS.md nearest each changed file,
   then every hunk of `git diff origin/main`. Look for:
   - bugs: wrong conditions, missed cases, races, errors swallowed, state left
     stale, a failure path that leaves the user stuck;
   - architecture: a decision made outside its owner, a patch or guard where
     the owning rule should change, a layer reaching past its boundary, a new
     path beside an old one it should replace.

   Fix each finding where the wrong decision is made.
   Evidence: the list of findings, each fixed or named in the report.

3. **Comments.** List every comment, docstring and prompt line the branch
   added or changed:
   `git diff origin/main -U0 | grep -nE '^\+.*(#|//|/\*|\*/|"""|<!--)'`.
   A comment is lasting knowledge about the code, something a reader needs
   as reference a year from now. It is never a one-off note about this
   feature or this change.
   Judge each line on the list. Delete it when it:
   - says what the code does, or repeats a name, type or return value;
   - tells the story of the change ("now", "instead of", "no longer", "previously", "new", "fix");
   - describes the feature, a dropped approach or a task;
   - is a section label or a divider.

   Keep it only when it states a non-obvious invariant, hazard or ordering, in
   one or two lines. Run what you keep through `unslop`.
   Evidence: the list again, with every remaining line one you can defend.

4. **Duplicate code.** For each helper, constant, condition and query the
   branch added, grep for an existing one that does the same job. Merge them
   into one, living with the owner of what it wraps, and point every copy at
   it. Remove any flag or param every caller passes the same way.
   Evidence: the greps.

5. **Dead code and files.** For each name the branch removed, renamed or
   stopped using, grep code, tests, docs and dynamic imports and delete what
   is left: helpers, exports, imports, constants, CSS, fixtures, files,
   settings, props, tests of deleted code. Remove scratch files, debug logs
   and test scripts. Remove planning, notes, status and analysis files an
   agent wrote, old ones included: markdown plans, task lists, findings,
   summaries. AGENTS.md files and skills stay.
   Evidence: the greps come back empty, and `git status` and
   `git diff origin/main --stat -- '*.md'` show no stray files.

6. **Docs.** For each behavior, rule, command, route or setting the branch
   changed, grep the README files, the AGENTS.md nearest the code and any
   other docs for text that describes it, and update what is now wrong. Add a
   line only for a rule or behavior a reader needs later, never a note about
   this change.
   Evidence: the greps, and each stale line fixed.

7. **Checks and PR.** Run `./check.sh`, then the steps in the workflows under
   `.github/workflows/` whose paths the branch touched (`go.yml`, `web.yml`,
   `python.yml`, `deploy-checks.yml`) and see each pass. A contract change
   also regenerates every binding. Then, only when asked for a PR:
   commit with one-line messages and no attribution lines, open the PR on
   `main` with the user's title and a few bullets on what changed, link it to
   the thread, and see its required checks pass.
   Evidence: the passing output, the PR link, green checks on the head commit.

## Subagents

Use them for a fresh read: one reviewing the diff for step 2, one judging the
comment list in step 3, one hunting duplicates and dead code for steps 4 and 5.
Give each the step text and the files, not your conclusions. You own every
edit, and you tick a step only after checking the agent's evidence yourself.

## Report

One line with the PR link and "N of 7 steps ticked", then at most three
bullets: what the review found and fixed, what the cleanup cut, and each
unticked step with why.
