# Spooldesk v0.1.0-rc.9

A prerelease for single-organization self-hosted installations. Use the
[installation guide](INSTALLATION.md) ([Русский](INSTALLATION_RU.md)).

## Changes from rc.8

- Installation confirmation uses `1` or Enter; `0` cancels. Unknown answers
  prompt again. Cancellation and end-of-input no longer appear as Docker failures.
- Password requirements are shown before entry. Length, unsupported characters
  and mismatched confirmation have separate messages. A manually entered password
  is not printed back; a generated password is shown once.
- Windows uses UTF-8 at the launcher boundary. Interactive and redirected input
  use the appropriate input stream. Browser launch failure does not undo a
  healthy installation; occupied ports and unhealthy containers stop clearly.
- The package is named `installer.zip`. Launchers and guides are at the top;
  technical payloads are in `package/`. macOS uses `INSTALL-MACOS.command`.
- Customer documentation has descriptive names and a documentation index.
  Development notes and agent tooling are excluded from source distributions.
- Tests exercise the real configuration dialog through native Windows/macOS/Linux
  launchers, including Russian input, validation, cancellation and retries.
  Real Docker installation/restart checks retain both classic and containerd stores.

The rc.8 config/OCI-manifest identity checks remain mandatory. The security
updates to multidict 6.9.1 and maintenance-tool uv 0.12.18 are retained.

## Installation and upgrade

Download `spooldesk-v0.1.0-rc.9-installer.zip` from this GitHub prerelease and
extract the entire archive. Open the launcher for your OS. The wizard requires
Docker; it does not require Python or Git installed on the host.

If a previous setup was cancelled before creating `spooldesk/`, use a fresh
extracted folder. If an installation already contains data, use the
[upgrade/backup procedure](DEPLOYMENT.md), not a new-install wizard.
Older release assets are not replaced. Existing installations are not migrated
or reset by this installer.

## Verification and limits of acceptance

`package/VALIDATION.json` identifies the exact source commit, images and CI run.
Artifacts are uploaded only after automated release checks pass. Native-shell
input tests use an isolated Docker substitute; real Docker image/installation
checks run on Linux, including ARM64 image startup under QEMU.
A successful native Windows/macOS end-to-end manual trial is not implied.
Manual installation acceptance on target computers remains pending.

## Scope

- PostgreSQL deployment, ticket queues/board, routing, customer verification,
  operator invitations and configurable section roles.
- Email, Slack, optional Telegram and website widget adapters, delivery queues
  and owner-only connection diagnostics.
- Conversation replies, supported channel edits/withdrawals, attachments,
  internal notes and waiting-for-customer SLA handling.
- Operator presence beside the reply composer: colleague names, public reply
  versus internal note, separate tab leases, expiry and no transmission of drafts.
  This is an advisory collaboration indicator, not an exclusive ticket lock.
- Optional AI assistant: scoped ticket-content analysis, confirmation cards,
  private persistent history, text/DOCX inputs and safe Markdown tables.
- English/Russian landing and dashboard, existing light/dark design.

## Known limits

Generic outgoing webhooks are deferred to a later update; they are not part of
this candidate. Existing channel-specific inbound handlers remain available.

One organization per installation; teams are routing groups, not tenant isolation.
Several request limits remain process-local and write transactions share a DB
lock; no horizontal scaling or capacity claim is made by this candidate.
AI upload formats: TXT/MD/CSV/JSON/LOG/DOCX, no image/PDF OCR. Analysis uses bounded
message samples and reports coverage. No arbitrary AI code execution or bulk
mutations. Channel capabilities differ; email withdrawal/edit is not guaranteed.

## Operations

For a public server, configure HTTPS, individual accounts, channel delivery
checks, backups and a tested restoration procedure. See [self-hosting](SELF_HOSTING.md)
and [deployment](DEPLOYMENT.md). The installer supports fresh setup and restart;
it is not an automatic upgrade tool. No stable-production or load-capacity
claim is made by this prerelease.
