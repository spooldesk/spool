# Spooldesk v0.1.0-rc.5 — GitHub installation candidate

Prepared 2026-10-02 for a single-organization self-hosted installation trial.
The GitHub prerelease identifies its exact commit; downloaded CI assets contain
`SOURCE_COMMIT`, `IMAGE_ID`, `IMAGE_TAG`, `VALIDATION.json` and `SHA256SUMS`.
The release is a candidate, not a claim of stable production acceptance.

## Changes from local rc.4

The previously local self-hosted candidate is prepared for a versioned GitHub
prerelease. CI now packages a Windows-friendly source ZIP, the tested image,
its tag, commit, validation provenance and an installation guide together.
Instructions cover downloading private GitHub release assets. One-off worker
tasks stay outside Git. Application behavior and dashboard design match rc.4.

The owner reports manual functional testing completed. Installation on a second
computer is pending; follow [SECOND_COMPUTER_INSTALL.md](SECOND_COMPUTER_INSTALL.md).
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

Download the assets listed in `SHA256SUMS` from the selected GitHub prerelease
and verify them before installation. The image must match `IMAGE_ID`; its tag
is in `IMAGE_TAG`. CI packages only after all dependent checks succeed.
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
