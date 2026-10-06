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

## Gaps and unverified boundaries
