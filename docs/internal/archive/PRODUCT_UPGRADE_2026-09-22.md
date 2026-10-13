# Spooldesk: PostgreSQL, conversations and operator lifecycle

This record supersedes the scope and readiness estimate in
`RELEASE_READINESS_2026-09-22.md`. It describes a single-company self-hosted
instance. No hosted SaaS, billing system or shared tenant database is implied.

The subsequent [dashboard review of September 24](DASHBOARD_RELEASE_REVIEW_2026-09-24.md)
records the canonical local database, session/AI fixes, ticket queues, compact
help controls and the scope of the latest validation.

## Product decisions

- PostgreSQL is the default deployment database, with schema revisions, separate
  attachment storage, verified backups and a rehearsed empty-target recovery.
  The project's disposable SQLite rows are not imported; the old file is preserved.
- A conversation is a ticket with ordered messages and optional reply references.
  Each message has delivery state, an optimistic edit version and an audit trail.
  An internal note never becomes a customer-visible quote or typing indicator.
- Provider capabilities govern actions. A uniform operator interface does not
  invent features that a channel cannot perform.
- One instance serves one company. Teams route assignments; they are not privacy
  boundaries. Operators can read other teams' tickets. Separate installations
  remain necessary for independent customers or strict data boundaries.

## Channel behavior

| Capability | Web widget | Telegram | Slack | Email |
|---|---|---|---|---|
| Reply | Local quote/link | Native message reply; quote fallback when no native ID | Existing ticket thread + quote of selected message | Existing Message-ID/References thread + plain-text quote |
| Edit operator text | Yes | Bot API, confirmed result | `chat.update`, confirmed result | No recall/edit after delivery |
| Edit file caption | Yes | Bot API caption limits | Disabled for file posts | No |
| Withdraw operator message | Local withdrawal | Bot API limits, including age | `chat.delete` for bot-owned posts | No |
| Visitor edits/deletes | Own active conversation; revision check | Inbound edits; ordinary Bot API does not supply general deletion events | Authenticated message-changed/deleted events | New mail is a new message |
| Typing | Visitor ↔ operators | Operator → customer; customer typing is not exposed by Bot API | Operator presence in Spooldesk; no fabricated native typing | Operator presence in Spooldesk only |
| Files | Queued preview + protected local storage | Native files | Native file API | Attachments |

Platform permissions, API limits and configured scopes still apply. Native
provider tests require dedicated real test accounts. Local tests use fakes and
cannot certify the configuration of somebody's Slack workspace or mailbox.

Relevant provider contracts: [Telegram Bot API](https://core.telegram.org/bots/api),
[Slack updates](https://docs.slack.dev/reference/methods/chat.update/),
[Slack deletion](https://docs.slack.dev/reference/methods/chat.delete/).

## Message lifecycle

Public replies persist an outgoing intent before network access. Failures are
visible; ambiguous sends require deliberate retry after checking the original
channel. Parent references must belong to the same ticket and be public,
available and delivered. Attachment selection shows previews before sending.
Image/PDF previews can open locally; arbitrary HTML/SVG uploads are downloads,
not executable same-origin previews.

Edits/withdrawals persist a mutation intent, release the DB lock for the provider
request, and update the transcript only on confirmed success. A stale optimistic
version is refused. A killed or timed-out request becomes `uncertain`, not a
silent success or automatic repeat. Owners/admins can check the original
conversation and use **Reconcile with channel** to record whether the change
happened. That action is audited and sends no provider request.

Operators see retained withdrawn messages and revision markers for continuity.
Widget visitors stop seeing withdrawn content. Agents may edit/withdraw only
their own operator messages; admins/owners can manage the conversation.
Incoming platform edits map by channel, correspondent and native message ID.

Presence is short-lived PostgreSQL state shared by dashboard processes. It
expires automatically and distinguishes an operator drafting a private note
from a public reply. It is activity feedback, not a record of work time.

## People and access

| Role | Scope |
|---|---|
| Owner | Instance settings, operators, invitations, team membership and all tickets |
| Admin | Settings, reports and tickets; cannot manage operator accounts |
| Agent | Tickets, customers and operational analytics; no configuration section |
| Viewer | Read-only tickets, customers and operational analytics; own preferences |

Owner-issued invitation links expire in 48 hours, work once and store only a
token hash. The recipient sets their own password. The link token is in the
fragment, then submitted by POST; it is not a query-string credential in access
logs. Acceptance checks creator authority again under the write lock. Old links
are revoked on replacement, owner demotion or account disablement. No invitation
email is sent automatically: the owner copies the link to their chosen channel.

Team membership limits assignment when a team has eligible members; owner/admin
can receive any ticket. A team without configured members retains the shared
queue fallback. Disabling an operator revokes sessions and releases open ticket
assignments. Password and role changes revoke sessions. The last active owner
cannot be removed. Deletion preserves conversation rows via nullable foreign
keys; newly sent messages retain a display label for author continuity.

Existing customer identity, account linking, blocking and removal paths remain
in place and are covered by the complete regression suite. This release does
not introduce public operator registration or a shared cross-company directory.

## Knowledge-grounded AI

The operator draft uses a bounded selection of published articles from this
installation. It applies the existing team/category policy and sensitive-text
scrubbing, and supplies only the question plus selected article excerpts to the
configured provider. It demands source IDs and displays links beside the draft.
Unknown citations, changed/unpublished sources or revoked policy withhold the
answer. The operator reviews it before sending.

This is retrieval from a knowledge base, not model training. There is no automatic
fine-tuning, file-crawl service or vector database. Article retrieval is currently
the existing search/candidate service; large imported corpora and measured answer
quality need their own evaluation set. Citations limit unsupported output but
do not prove each generated sentence. Live provider calls were not made in tests.

Existing AI cost/savings displays explicitly state their assumptions. Generating
a draft is not counted as a delivered customer answer or measured saved work.

## Reports and appearance

The report separates the **current queue** from the selected reporting period:
open/waiting tickets, customers awaiting an answer, SLA breaches, oldest ticket,
and deliveries needing attention. First-response median/p90 use delivered public
replies for tickets created in the period. Resolution uses the actual closing
timestamp for tickets closed in the period; it includes waiting time. Overview,
ticket duration and reports now use the same closure definition. Sample sizes,
UTC snapshot time and empty observations are explicit. Excel includes four sheets.

Landing EN/RU and the cabinet support light/dark appearances. Landing themes
follow the system initially and persist an explicit choice. The workspace uses
neutral panels, compact controls and consistent message actions. Reduced-motion
preferences are respected. The widget follows the visitor's system appearance.

## Delegation and verification

Claude Code implementation roles now return bounded file artifacts into dedicated
git worktrees/branches. The runner checks an explicit base commit, path allowlist
and selected snapshot hashes. It never writes the main working tree automatically.
Lead reviews the artifacts, integrates accepted code and runs tests. Tool-less
workers cannot execute recursive agents or shell commands.

Conversation-policy and knowledge-draft artifacts were reviewed and integrated.
Two other implementation attempts returned malformed JSON and were not retried
automatically; Lead completed those areas. A later account-security review was
partly disproved by actual schema/session settings and recorded as rejected,
with its one confirmed read-lock improvement applied. The local delegation log
records each attempt and acceptance evidence without full prompts or secrets.

### Verification evidence (22 September 2026)

All checks used synthetic data and isolated settings; no live customer messages,
provider credentials or the installation's real `.env` were used.

| Check | Result |
| --- | --- |
| Full suite, Windows / Python 3.12 | 1,196 passed; 28 subtests passed |
| Full suite, PostgreSQL 16 / Linux | 1,191 passed; 5 skipped; 28 subtests passed |
| Final targeted regression suite after the polling-order fix and three added tests | 53 passed on Windows; 53 passed on PostgreSQL |
| Production image smoke, PostgreSQL | Health 200; database and queues healthy; UID 10001; background workers enabled; no source bind mount |
| PostgreSQL backup and fresh-volume restore | Passed in 28.2 seconds on the small synthetic fixture |
| JavaScript / landing build | `node --check` for conversation and landing scripts; generated EN/RU pages current; public-only export created |
| Browser workflow | Operator edit, reply, withdrawal; visitor attachment preview/upload; customer typing observed in operator view; reports, roles UI, both appearances inspected |
| Responsive check | RU landing and ticket view fit a 390px viewport without horizontal page overflow |

The recovery drill checked schema, rows, attachment bytes, authentication,
encrypted settings, quarantine of outstanding incoming/outgoing/message-mutation
jobs, restart of the original writer and stopped writers after restore. Its
duration is a fixture result, not a production recovery-time promise.

The full suites precede the last polling-order change; the final targeted runs
cover that change and the new regression tests. Existing deprecation warnings
remain. The image tested after that change is
`spooldesk:upgrade-review-20260922`, image ID
`sha256:9b60ad79279bb27f19b0a5070338f978d08b064341a1a08c78f8cd676eebfe68`.

Local evidence is retained under ignored `.agent-runs/`: `upgrade-windows-final.log`,
`pg-final-console.log`, `upgrade-last-checks.log`, `pg-last-checks.log`,
`upgrade-image-smoke.log` and `pg-recovery-drill.log`. A local test pass is not a
published release or a provider account configuration test.

## Remaining gates for a public release

1. Commit a reviewed release candidate and run the full CI pipeline on that exact
   revision. This session preserves the existing dirty tree; it does not commit,
   push or publish it.
2. Smoke-test configured Telegram, Slack, SMTP/IMAP and the chosen AI provider on
   dedicated test accounts. Include edits, replies, files, disconnects and retries.
3. On the actual installation: domain/HTTPS, preflight, independent backup storage,
   recovered encryption key, external monitoring, alerts and a restore rehearsal.
4. Run a representative concurrency/volume test and a small supervised pilot.
   Current process-shared mutation locking targets small support teams; PostgreSQL
   alone is not evidence of unlimited horizontal scale. Several web rate limits
   remain process-local, so place an appropriate edge limiter in front of public
   endpoints if running multiple dashboard replicas.
5. Package/version the tested container and source release, publish only the public
   landing export, and measure real operator workflow and knowledge-answer quality.

Paid general availability should follow these gates. The result is a locally
verified candidate for a controlled self-hosted pilot, not an unconditional
claim that every production installation is ready.
