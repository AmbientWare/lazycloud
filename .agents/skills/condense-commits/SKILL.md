---
name: condense-commits
description: Rewrite the current branch's history into a few small, direct commits. Undoes every commit since the branch left `origin/main`, regroups the changes by what they do, and commits each group with a one-line message, then pushes when asked. Use when the user asks to condense, squash, regroup, clean up or tidy a branch's commits.
---

# Condense commits

A branch built over many sessions carries fix-ups, reverts and "trim" commits.
This pass leaves the same final tree in a short history a reviewer can read
commit by commit. How you group and stage is up to you.

Scope is the current branch against `origin/main`. Everything since the merge
base is fair to rewrite; nothing on main is.

## Rules

- **Same tree.** The last new commit's tree matches the branch's tree before the
  rewrite, byte for byte. Check it with `git diff <backup> HEAD`, which must be
  empty. Uncommitted work is either committed in a group or left as it was.
- **Backup first.** Keep a backup branch at the old head until the push lands and
  the diff check passes. Never bare `git stash`.
- **Groups by purpose.** Each commit is one change a reviewer can judge alone:
  a migration and its queries, a contract and its regenerated bindings, a UI
  flow, a test. Split by what the code does, not by file or by when it was written.
  Earlier attempts the branch later dropped appear nowhere.
- **Few and direct.** Prefer three to six commits. A commit that only fixes or
  undoes another in the same rewrite merges into it.
- **Each commit stands.** Order the groups so each commit imports and passes
  lint on its own where the split allows it. The regenerated Go, Python
  and TypeScript bindings go in the commit that changes
  `contracts/openapi.yaml`.
- **Repo commit style.** One-line messages in the repo's style, with no
  co-author or attribution lines.
- **Safety.** Rebase onto the latest `origin/main` first if the branch is
  behind. Push only when asked, and only with `--force-with-lease` to the
  branch's own remote. Never rewrite `main` or another person's branch. A
  refused tool call is a stop.

## Report

One line with the new commit list (short hash and message), then whether the
tree check passed and whether the branch was pushed.
