# Image conversion in the task lifecycle

## Scope

A start that waited 9 s for an on-demand image conversion showed as
"Queued 8.9s" on the task page. Show the wait for an image conversion or
build as its own lifecycle stage (contract, execution's stage recording,
the web task page and its lifecycle bar), the same look as the other
stages. Owns those; one migration file if the stage needs storage.

## Evidence to record

- The task page for a start that waited on a conversion, showing the stage.
- Contract and bindings regenerated; SDK and CLI unaffected or updated.

## Progress

- The session records a `conversion` start stage (migration 0005 widens
  the stage check) for a start that waited for its image, from the
  container's assignment to the start sent, or to its permanent failure. `TestStartWaitsForTheManagedImageConversion`
  proves it.
- `LifecycleStageKind` gains `conversion` and `disk` (the host already
  reported disk). Go, Python and TypeScript bindings regenerated; the SDK
  and CLI do not show stages.
- The task lifecycle bar shows "Image conversion" between Queued and
  Container preparation (`LifecycleStrip.test.tsx`).

## Gaps and unverified boundaries

- A session that ends while a start waits records nothing; the session
  that sends it stores the whole wait from the assignment. If the image is
  ready before the next session's first sync, that session sends the start
  without a wait, nothing is stored and the time shows as Queued.
- hostsession's object-store tests need the test-store packet's store;
  they fail locally on a missing `lazycloud-layers` bucket, unrelated here.
- Not seen on a real deployed task page yet; acceptance covers it.
