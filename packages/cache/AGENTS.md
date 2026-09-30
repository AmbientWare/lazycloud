# Cache

- Own transient coordination and content-addressed runtime data; durable history
  belongs to its durable owner and byte storage stays behind storage protocols.
- Verify hashes, sizes and completeness. Publish atomically and preserve reader,
  writer and cleanup safety under concurrency.
