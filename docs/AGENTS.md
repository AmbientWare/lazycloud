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

## Shape and voice

The introduction pitches LazyCloud as infrastructure for agentic engineering:
a workload's Python decorator is its whole definition, and there are no CLI or
dashboard overrides. Readers go from the introduction, quickstart, and
installation into Core concepts, where each page explains how a part works and
how to use it in the same place, then to the CLI reference, examples, and
account pages. Agent-friendliness comes from one-command steps, `--json`, and
exit codes throughout, not from a separate section.

Each page does one job, and each fact has one home that other pages link to.
Pages describe what a thing is for and when it fits; imperatives belong only
to literal steps in the quickstart and example guides and to CLI syntax. A
page's frontmatter description renders above the body, so the first paragraph
never repeats it. No sentence describes the page itself.

Examples run as downloaded. They show `.local()` beside `.remote()`, a plain
`python file.py` beside `lazycloud run module:function`, and say plainly when
code runs or deploys in the cloud rather than on the reader's machine. GPU
examples use `GpuType`. Internal runtime details, such as environment
variables the platform sets or wire identifiers, stay out of the docs.
