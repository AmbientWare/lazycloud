---
name: release-notes
description: Make a shareable dark-mode PDF overview of what a PR or feature changed, with screenshots and crops of the real app and short bullets per area. Use when the user asks for release notes, a feature overview, a changes doc or PDF for coworkers or customers, or to update that doc after a PR.
---

# Release notes

The output is one portrait PDF that reads like a PR description with pictures:
a title, a one-line lead, then a section per area of the screen with a few
plain bullets and the screenshots that show them. It goes to coworkers, so it
says what changed and how to use it. How you capture and assemble it is up to
you. The finished PDF must meet the rules below.

Scope is `/release-notes [PR number or feature]`, or the current branch against
`origin/main` with no argument. When an earlier doc exists, update it rather
than starting over.

## Rules

- **Audience first.** Write what a user sees and does. Leave out internals
  (state owners, line counts, request counts, timings) unless the user asks.
  No PR numbers in customer-facing copies.
- **The UI's own words.** Labels and names match the components exactly.
- **Simple bullets.** One fact per bullet, a few per section, written with
  `unslop`. Captions name what the picture shows and don't repeat the bullets.
- **Each fact once**, in the section that owns it. Read the built text back and
  cut repeats.
- **Real screens only.** Capture the running app. Never fake data or mock a
  state. If a state (a container starting, say) isn't live, create it for
  real on local data, or leave it out and say so. Never touch production data.
- **Every shot earns its place.** It shows its point clearly, with the feature
  filled in: panels it uses are on and loaded, nothing half drawn. Drop or
  recrop any that don't.
- 1600x1000 viewport, 2x pixels. Full screens for context, crops
  for menus, popovers and controls.
- **Checked before delivery.** You've looked at the page layout and read the
  text back, and fixed what was off.
- **Delivered to the project root** as `<feature>-overview.pdf`, untracked.
  Never commit it. Report the path and anything left out, with why.

## Tools in this skill

Use them or not. They encode what has worked.

- `scripts/`: run `bun install` there once. Needs Node, Python with Pillow, poppler
  (`pdftoppm`, `pdftotext`) and Chromium in `~/.cache/ms-playwright`
  (`bunx playwright-core install chromium`).
- Sign-in is a session token from a local server on a task-owned database:
  `server admin create-session --email <user> > <session file>`. The session
  cookie is `Secure`, so a browser on plain `http://localhost` drops it; the
  launcher adds it to each API request instead.
- `scripts/capture.mjs <page url> <steps.mjs> <session file>` with `OUT=<dir>`
  and `READY=<selector or text to wait for>` runs a steps module (see
  `references/steps-example.mjs`) and writes the shots.
- `scripts/contact.py <sheet.png> <images or pdf> --cols N` makes a contact sheet
  to review shots or pages; `scripts/crop.py` recrops in CSS px.
- `scripts/build.py notes.md out.pdf --font web/node_modules/@fontsource-variable/instrument-sans/files/instrument-sans-latin-wght-normal.woff2`
  builds the PDF and a standalone HTML. The notes format is in its docstring:
  `# Title`, lead line, `## Section`, `- bullet`, `![caption](/abs/path.png)`,
  `::: row` / `:::` for crops side by side, backticks render as keys.

## Capture hazards

- Find controls by aria-label first.
- Dropdown menus portal out of dialogs; locate their contents on `page`.
- Settings tabs open from the URL, for example `?settings=admin`.
- Wait on state (a label, a selector), not long sleeps.

## Subagents

Use them where they help, for example capturing several areas in parallel or
a fresh read of the text for repeats and jargon. You own the final PDF.
