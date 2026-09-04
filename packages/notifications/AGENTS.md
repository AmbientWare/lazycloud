# Notifications package

Transactional email: the outbox the platform queues messages into, and the drain
that hands them to a provider.

Queueing is a database write in the caller's own transaction. Nothing that
serves a request ever waits on the email provider, because a provider having a
slow morning would otherwise become an API having a slow morning, and every
handler that mentions email would hold a worker while it did. The row and the
thing the message announces commit together, so there is no state where somebody
was told about a change that then rolled back, and none where a change happened
that nobody was ever told about.

The drain derives nothing. It claims rows that already exist, sends them, and
marks them. The rendered message is what was stored, so what goes out is what
the action that queued it meant to say rather than what the same code would
render today.

Delivery is at-least-once and the provider is the one deduplicating. A claim is
taken with `FOR UPDATE SKIP LOCKED` and spends its attempt at claim rather than
at failure, so a drainer that dies mid-send burns one attempt instead of
spinning on the same row. A claim left behind by a dead process is reclaimed on
a timeout. A message that exhausts its attempts is abandoned rather than
retried forever, and the standing abandoned count is what an operator watches:
it is the number of people who were never told something.

Sent rows are pruned. They hold the rendered message, and an invitation's
message holds a working link, so retaining them indefinitely would keep those
links readable long after the mail was the only place they lived.
