# Cache Package

Typed hot coordination and content-addressed runtime data.

Redis here is coordination, not durable history—anything that has to survive
belongs to a durable owner. Object and filesystem responsibilities stay behind
storage protocols rather than being reimplemented.

Content-addressed data is only as trustworthy as its verification. Validate
hashes, sizes, and completeness; materialize atomically so a partial write is
never visible as a finished one; and make concurrent readers and writers, and the
cleanup that follows them, explicit rather than assumed.
