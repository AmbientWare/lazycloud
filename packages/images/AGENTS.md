# Images

- Own image planning, lifecycle, build execution, publication and cleanup through
  narrow container, scheduler, storage and secret protocols.
- Project images install dependencies, including declared local dependencies, but
  skip root project code supplied by source sync. Bump the build identity contract
  when inputs change. Managed runtimes are content-addressed per Python minor.
- Publish only complete builds. Fence credentials, progress and results to the
  current execution attempt; retired containers cannot write into its successor.
- Confirmed provider interruption permits one restart within the original deadline
  while durable inputs exist. Command failures/timeouts fail; cancellation prevents
  retry. Each execution attempt retains its own billing, logs and upload identity.
- Cleanup names the retired attempt and respects upload capability expiry;
  globally retained image archives survive attempt cleanup.
