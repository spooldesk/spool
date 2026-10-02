# Dashboard review — 2026-09-24

This update continues the single-company self-hosted release described in
`PRODUCT_UPGRADE_2026-09-22.md`. It does not certify a public production release.

## Local workspace

The selected workspace is `spooldesk-preview`, PostgreSQL, port 8011.
The final read-only count was 87 tickets and four teams: 13 active and 74 closed.
No reseed, database switch or credential rotation was performed. See
`../LOCAL_PREVIEW.md` for the single launch configuration.

## Ticket opening and queue

The reported HTTP 500 came from a newly loaded ticket template serializing
missing translation keys while the running Python process still held the older
catalog. Restarting the dashboard loaded matching code; the composer now also
falls back to an existing error translation when those keys are absent.
An HTTP regression test exercises that missing-key case.

The default ticket queue contains active work. Closed tickets have a separate
tab, count, pagination and export. Switching tabs preserves common filters and
resets pagination. Work stage applies only to active tickets. The archive keeps
closure time, duration and CSAT columns; the active queue omits those empty
columns. A closed ticket's back link returns to the archive.

Explanatory copy across dashboard settings, reports, knowledge management and
conversation controls now uses small circled information buttons. Help opens on
hover, keyboard focus or click and closes with Escape or outside interaction.
Panels fit the viewport in both themes. Text remains available without JS.
Errors, current configuration state and consequences of destructive actions
remain visible.

## Other fixes in this review

- Expired form sessions use an appropriate login redirect; API/fetch requests
  receive JSON 401. The conversation composer keeps unsent text and attachments
  when the session expires or a submission fails. It prevents duplicate
  in-flight submissions and does not automatically retry an uncertain send.
- AI provider changes keep model/host drafts separate and clear unsaved API
  credentials. A configured key is no longer described as a verified connection.
  The AI button requires an enabled, usable provider and applicable policy.
- Advanced AI trial and savings settings are grouped. Non-finite cost values
  are rejected, and settings audit entries identify the actual operator.
- Telegram-only navigation is conditional on Telegram configuration; the
  product remains multichannel. The duplicate AI trial navigation was removed.
- Shared widget assets are embedded in its frame again. Static asset versioning
  covers all CSS/JS content, preventing stale scripts after application restart.

## Verification and limits

- Earlier full isolated PostgreSQL run: **1,219 passed, five skipped**, plus
  28 subtests. This preceded the final composer and queue/help changes.
- Subsequent targeted PostgreSQL run: **222 passed**, covering ticket queues,
  stale translations, composer, security, uploads, workflow, saved views,
  board, localization, reports, AI configuration and widget routes.
- Current frontend suite: **eight passed**, including session expiry, CSRF,
  provider switching, draft preservation and duplicate-send prevention.
- Browser checks: active and closed ticket opening; archive return;
  help disclosure/Escape without changing a checkbox; light/dark themes;
  help panel and page fitting a 390-pixel viewport.
- Earlier browser conversation checks used a disposable database: send, edit,
  file selection, expired session, reauthentication and one successful retry.
  The temporary app and database were removed. The canonical demo was not used
  for mutation tests or live provider traffic.

The final minor help-marker and CSS changes were checked in the browser;
the earlier full-suite result must not be represented as a run of every later
revision. Existing unrelated uncommitted work was preserved.

## Claude contribution

One bounded read-only call, `dashboard-ai-review-20260924`, selected four AI
files; no repeated calls. Reported model cost: $0.1587818. The Lead verified
and accepted the misleading connection label and settings grouping findings.
The alleged blacklist case-loss bug was rejected because both persistence
directions normalize case. Zero customer delivery in trial mode is intentional,
not a broken metric. The provider-draft bug was found by the Lead separately.
Detailed invocation and acceptance records are local in
`.agent-runs/delegations.jsonl` (ignored by Git).

## Release gates still open

Produce a reviewed, versioned release candidate with CI on that revision.
Exercise dedicated real provider accounts and failure/recovery paths; local
fakes cannot validate a customer's Slack scopes, mailbox or AI credentials.
Rehearse the target installation with HTTPS, monitoring and off-site backup
recovery. Run representative concurrency tests and a small customer pilot.
These requirements do not imply that the owner must run a SaaS server to sell
a self-hosted edition.
