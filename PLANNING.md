# Planning large changes

Use this method for work that spans several owners, needs more than one PR, or
runs parallel agents. The Go rewrite, the fleet capacity work and the
performance work ran this way.

## Start from evidence

- Measure the problem first and write the numbers down. A plan without a
  measured reason is a speculative addition.
- Record the user's decisions with their date, and the integrator's defaults
  with a note that the user may overrule them.
- The bar is the same product or better for the user. Record every intentional
  difference with its reason.

## The plan branch

Create `<topic>-plan` from main. It holds `tasks/<topic>/` and is the
integration branch for the packets. It never merges to main as is: the final
PR removes `tasks/<topic>/` first.

- `README.md`: the decisions, the packet table (packet, branch, migration
  number, shared-file range, dependencies), the waves, the agent rules and the
  merge gate.
- `plan.md`: the goal, the design, how the reference or a comparable system
  does it, and the open questions.
- One file per packet: scope, the files it owns and the areas it stays off,
  the plan, the evidence it must record, progress, intentional differences,
  gaps and unverified boundaries.

## Spike the unknowns

When the design rests on something unproven, the first packet is a spike: a
short list of yes-or-no questions answered with command output and numbers,
in scratch code that never merges. A no stops the plan and goes back to the
user with options. Refine the later packets from the spike's report before
they start.

## Packets and waves

- Split by owner, not by entity. Each packet gets its own migration number and
  its own range in shared files (protobuf field numbers, API paths).
- One integrator owns shared definitions, root build files and the plan files,
  and tells running agents when the base moves.
- Packets with no dependency run in parallel. A small packet that the rest do
  not need goes to main on its own.

## Briefing agents

Every brief says:

- what to read first, the branch and its base commit;
- the goal and the bar;
- the files the agent owns and the areas it stays off;
- whether sub-agents are allowed (default no, never an unannounced fan-out);
- the toolchain paths and how to verify;
- cleanup duties: its own worktree, compose project, ports and state
  directory; real EC2 in `AWS_PROFILE=default`, tagged with the packet and
  terminated before the report; never touching resources it did not create
  or stopping shared services;
- commit and push after each meaningful step, no PRs;
- a report length limit and what the report contains.

Agents stop when sessions end. Their pushed branches are the record: resume a
stopped agent first, or start a fresh one on its branch.

## The merge gate

Each packet passes this before it merges into the plan branch:

1. `./check.sh` and focused `go test -race` on the touched packages, with
   visible output; generated code current; clean tree.
2. A separate read-only reviewer with a focus list written for the change. It
   reports only verified defects with a file, line, failure scenario and fix.
3. Every real finding fixed, with a test that fails without the fix.
4. Every check passed and none pending. Squash with a one-line subject.

## Finishing

Merge packets into the plan branch in dependency order. Then review the whole
plan branch against main, remove `tasks/<topic>/`, open one PR, and give it
the final gate: full review, dead and repeated code removed, prose condensed
and unslopped. Ship once with the user's go-ahead, run the acceptance steps in
prod, and clean up every resource the work created.
