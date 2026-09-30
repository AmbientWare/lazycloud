# Gateway

- Own gateway control and backend routing behind narrow protocols. App routing
  and process composition stay outside; RPC contracts live in shared HTTP.
- Preserve workspace authorization throughout streams, reconnects and route cleanup.
- Verify provider identity before enrollment's write transaction. Commit launch
  claim, credential, machine, worker, enrollment and binding in one session.
  Publish Redis state after commit; recover retries from durable authority.
- Join credentials bind the recorded machine and workspace list. Reissuing a
  pending join revokes its predecessor; an already joined name conflicts.
  Do not remove a served workspace with a deployment pinned to that machine.
- Use the compute lifecycle owner for transitions. Disconnect updates connectivity,
  not phase; classify machine views by durable placement.
