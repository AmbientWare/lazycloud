# Test object store

## Scope

The storage, image and agent tests default to the user's `lazycloud-local`
Garage on port 23900, and they leave a bucket behind per run (535
`lazycloud-test-*` buckets on 2026-10-05). Owns the test configuration
(`internal/storage/storagetest` and its users) and `compose.test.yaml`.

## Plan

- A Garage service in `compose.test.yaml`, beside the test Postgres, that
  tests use by default; the user's development stack is never a default.
- Tests delete the buckets and objects they create.
- After that lands, delete the leftover `lazycloud-test-*` buckets from the
  user's `lazycloud-local` Garage (the user approved on 2026-10-06), and
  nothing else in that stack.

## Evidence to record

- A full test run leaves no bucket behind in the test Garage.
- The count of leftover buckets removed from `lazycloud-local`.

## Progress

- 2026-10-06: `compose.test.yaml` runs Garage on 15900 (admin 15903) with a
  bootstrap that stays up healthy for `up --wait`. `storagetest.Config(t)`
  makes a platform bucket, a layer bucket and a workspace prefix per test
  and deletes them with their objects; `storagetest.Open` does the same per
  test binary for the agent and acceptance TestMains. CI test jobs use it
  (PR #503, all jobs green).
- The CI validate test set run locally left no bucket in the test Garage.
- Removed 579 leftover `lazycloud-test-*` buckets from `lazycloud-local`,
  emptied first; only `lazycloud` remains there.

## Gaps and unverified boundaries

- The agent harness and acceptance ran in CI only, not on this machine.
- Branches still on the old `storagetest` keep writing to whatever
  `LAZYCLOUD_TEST_OBJECT_STORE_ENDPOINT` names until they merge this.
