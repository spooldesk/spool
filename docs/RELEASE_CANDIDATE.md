# Spooldesk v0.1.0-rc.8 — GitHub installation candidate

Prepared 2026-10-13 for a single-organization self-hosted installation trial.
The GitHub prerelease identifies its exact commit; downloaded CI assets contain
`SOURCE_COMMIT`, `IMAGE_ID`, `IMAGE_TAG`, `VALIDATION.json` and `SHA256SUMS`.
The release is a candidate, not a claim of stable production acceptance.

## Changes from rc.7

- Fix the installer's `Loaded image differs from the tested image` error on
  Docker Desktop's containerd image store. The package derives both config and
  OCI manifest digests from the same archive; launchers verify membership and
  platform before any container is created or run. Checksums remain mandatory.
- Add real installation/restart testing on a separate containerd engine, as well
  as rejection tests for unrelated IDs, wrong platforms and tampered metadata.

## Installation introduced in rc.7

- A terminal installation wizard for Windows, macOS and Linux replaces manual
  command copying. Docker is the only host prerequisite; Python runs offline
  inside the verified application image.
- Launchers check the package, local engine/architecture and existing resources;
  setup generates isolated credentials and asks for the dashboard port. Repeated
  launch preserves credentials and storage and refuses conflicting installations.
- Compose supports a configurable loopback port and labels volumes with the
  installation identity. The dashboard design and application features are unchanged.
- CI adds native-shell tests on all three OS families and a real Linux Docker
  installation/restart test using the extracted release ZIP.
- rc.6 security patches remain: multidict 6.9.1 and maintenance-tool uv 0.12.18.
  Both amd64 and arm64 images are rebuilt; ARM64 startup is tested under QEMU.

Older assets remain unchanged. This installer is for new installations or
restarting its own installation, not an automatic upgrade/migration wizard.

The owner reports manual functional testing completed. The first second-computer
installation stopped at the image identity check in rc.7 on 2026-10-13. Acceptance
is pending a retry; follow [SECOND_COMPUTER_INSTALL.md](SECOND_COMPUTER_INSTALL.md).
Publication is authorized as a prerelease for that trial. Website deployment
and paid services are separate. Follow the CI link in `VALIDATION.json` for
checks on the released commit; do not substitute old local evidence for them.

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

## Candidate artifacts and checks

Download and extract the transfer ZIP from the selected GitHub prerelease and
verify its `SHA256SUMS`. The loaded ID must match a digest in `IMAGE_IDS` (or
`IMAGE_IDS_ARM64`); its tag is in `IMAGE_TAG`. The two accepted digests identify
the linked config and OCI manifest of the same archive. `IMAGE_ID` records the
builder's ID. CI packages only after all dependent checks succeed.
Local working-tree packages created with `scripts/build_release_candidate.py`
remain a separate workflow and must not be confused with Git commit archives.
A runtime change needs a new candidate and matching checks, not silently
replacing files in an already tested release.

The canonical preview stays at port 8011 on its existing PostgreSQL volume.
Candidate tests/installations must use disposable databases and separate ports.
Never use preview credentials, demo keys or seeded AI TEST data for production.

## Per-client deployment checks and remaining release gates

- Verify receive → ticket → reply → customer receipt → file → follow-up → close
  for each promised channel, including provider disconnects and duplicate events.
- Check two operator accounts on the same ticket, roles/view-only access,
  disabling an operator and customer verification/team assignment.
- Evaluate the selected real AI provider on representative questions, cited
  sources, files and confirmations. Automated tests use fake providers.
- Install the candidate on empty storage, run production preflight, verify
  HTTPS/cookies and keep monitoring outside that machine.
- Restore a current backup (PostgreSQL + attachments + original separate key)
  onto new storage. Inspect quarantined delivery before sending is enabled.
- Run representative concurrency/volume checks, review dependencies and source,
  then freeze a reviewed Git commit and pass CI on that exact revision.
- Publish a versioned source/container only after review. Define operator
  support hours, update ownership and backup/restore ownership for the pilot.

## Known limits

Generic outgoing webhooks are deferred to a later update; they are not part of
this candidate. Existing channel-specific inbound handlers remain available.

One organization per installation; teams are routing groups, not tenant isolation.
Several request limits remain process-local and write transactions share a DB
lock; no horizontal scaling or capacity claim is made by this candidate.
AI upload formats: TXT/MD/CSV/JSON/LOG/DOCX, no image/PDF OCR. Analysis uses bounded
message samples and reports coverage. No arbitrary AI code execution or bulk
mutations. Channel capabilities differ; email withdrawal/edit is not guaranteed.

## Upgrade / rollback

Stop writes and make a verified backup before migrations. Keep the previous
image and pre-upgrade backup. Roll back with that backup on new storage; do not
assume an older image supports a newer schema. Run only one live writer instance
to avoid duplicate messages. Follow POSTGRESQL.md and RELEASING.md.
