# Documentation

`docs/` is a Mintlify MDX project configured by `docs.json`. Every page is
reachable from navigation, and content used in more than one place lives in
`snippets/`.

Document only current public SDK and CLI behavior, grounded in the code and in
real help output rather than in intent. LazyCloud is a hosted platform: what a
reader connects is compute they own, so installing or operating the platform
itself is not documented here, and neither is the internal operator CLI.

Keep examples runnable. Keep configuration truth with its typed owner and
reference it instead of restating it, so there is one place to be wrong. Document
secret names and how they are injected, never their values.

When behavior goes away, delete the page that described it rather than building a
parallel registry of what is still true.
