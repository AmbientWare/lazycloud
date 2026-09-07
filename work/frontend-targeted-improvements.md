# Targeted frontend improvements

This draft implements the pricing copy change below. The remaining work is proposed. Based on main at `c446ab43`, including the log streaming and retention work in #155. Re-check each issue against current main before implementing it. Do not replay #153 wholesale.

## Design constraints

Preserve the existing clouds, animated demonstrations, transitions, typography, headline scale, spacing, panel dimensions, section order, illustrations, and responsive composition. Keep the current components' appearance while fixing their behavior.

Copy edits should remove a specific repetition or confusing instruction. Keep useful product explanations, examples, pricing terms, and calls to action. Preserve the layout around edited copy. Review operational status indicators individually; keep actual loading, failures, connection loss, and consequential billing or permission information. Ambient motion and animated demonstrations remain part of the design.

Measure rendering and network costs before optimizing them. Pause unnecessary work when hidden or offscreen and respect reduced-motion preferences while preserving the intended experience when visible.

## Proposed implementation order

1. **Pricing, one focused change.** This draft removes the "Compute region" selector, its helper copy, the effective-date caption, and region-selection rows in the plan cards. It uses the catalog's automatic-placement rates and links to regional pricing details in the docs. Hourly/secondly controls, other plan entitlements, log-retention terms from #155, and billing disclosures remain. Typography, card styling, and animations stay as they are. Owner: [pricing page](../apps/web/src/routes/pricing.lazy.tsx). Acceptance: desktop/mobile presentation and correct catalog-derived prices in both units. Loading and failure recovery remain a separate proposed change.

2. **Workspace rename.** Fix the idle/editing mismatch that prevents input changes. Renaming a different workspace must not move the current resource into that workspace's URL. Preserve the dialog's appearance. Owner: [WorkspaceRename](../apps/web/src/components/shared/WorkspaceRename/). Acceptance: type, save, cancel, recover from an error, and rename current/other workspaces while viewing a resource.

3. **Sign-in and session recovery.** Distinguish an unavailable API from an expired session, offer retry, and preserve the requested destination. Check single-use callback redemption and activation redirects. Owners: [AuthGate](../apps/web/src/components/shared/AuthGate/), [callback](../apps/web/src/routes/callback.tsx). Acceptance: outage, expiry, callback failure, and return navigation through the real auth owner.

4. **Shared interaction feedback.** Handle clipboard failure; retain button labels while pending; prevent duplicate submissions; connect labels and validation errors to fields. Extend existing Button, CopyButton, and error components first. Extract a new component only when two concrete consumers need the same behavior, and migrate those consumers together. Acceptance: keyboard use, rejected copy, failed/repeated submission, and unchanged control dimensions.

5. **Secret and file request ownership.** A late secret reveal must not undo Hide or reappear during deletion. A late file read must not replace another selection or reopen a preview after navigation. Show transfer errors separately from file contents. Keep existing file-browser layouts. Implement bounded previews through the backend owner in a separate contract change; preserve complete downloads. Acceptance: rapid selection changes, hide/delete during reveal, failed transfers, and a large preview with bounded bytes read.

6. **Workload and machine correctness.** Keep current deployment selection independent of paginated history. Verify same-name workloads of different kinds remain distinct for navigation and destructive actions. Correlate machine enrollment with the actual join attempt instead of an existing host's recent heartbeat. These are separate API/domain changes with synchronized web and SDK contracts. Acceptance: browse beyond retained history without changing the invoke target; delete only the intended workload; generate a join command without running it and confirm an existing host cannot complete it.

7. **Large lists and logs.** Make map keys beyond the first 100 reachable; use bounded search across all relevant records; obtain authoritative running counts. Preserve loaded records when a later page fails. Keep #155's real log stream and retention behavior; investigate the remaining rendered-line cap and scroll/follow behavior before changing the viewer. Handle each owner separately. Acceptance: records beyond the first page, continuation retry, older log navigation, live arrivals, and stable reading position.

8. **Responsive and accessible interactions.** Keep long dialogs, invoke forms, results, and terminal controls reachable on small screens. Preserve keyboard focus during live updates and choose a valid tab when available actions change. Add terminal reconnect where a disconnected session otherwise leaves no recovery. Fix legal-page printing without changing screen presentation. Acceptance: keyboard navigation, narrow/landscape layouts, long content, reconnect, and complete printed documents.

9. **Copy and status pass across pages.** Review marketing, sign-in, settings, app/workload/task pages, sandboxes, storage, and usage. Identify exact duplicate subtitles, badges, and status indicators, then propose small edits with before/after screenshots. Preserve animations, demonstrations, useful explanations, page hierarchy, and readable sizing. A count of removed words or components is not a success criterion.

10. **Production rendering and performance.** Reproduce the direct-navigation hydration error observed during the previous deployment through the API's actual static-file serving path, not only Vite preview. Measure hidden/offscreen animation work, repeated polling, and large-list rendering before selecting fixes. Acceptance: direct route loads and refreshes, correct initial HTML, no new console errors, and measured improvement without changing the visual design.

## Delivery and review

This draft changes pricing alone. Implement the remaining items as focused PRs, one coherent behavior at a time.

Capture the current desktop/mobile presentation before each visible change. Compare the same routes, viewports, scroll positions, and interaction states afterward. Record intentional copy differences; restore any accidental change to scale, spacing, or motion.

Run the narrow owner checks and the named customer workflow for each change. Add automated tests only for material behavior not already proven more cheaply. Have a normal code review and an unslop review check both the result and adherence to this scope. Leave the visual pass with the owner before merging or deploying visible changes.
