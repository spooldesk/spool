# Self-hosted release procedure

Release one reviewed commit and one tested image. A branch is not an installation;
an unpublished working tree is not a release. This procedure does not require
the maintainer to rent a VPS.

## Prepare the candidate

For a local candidate before Git review, use
`python scripts/build_release_candidate.py --version v0.1.0-rc.4 --output .agent-runs/release-candidates/v0.1.0-rc.4`.
This captures allowed tracked and untracked source into `source/`, a ZIP and
`SOURCE_MANIFEST.json` with every file hash. It excludes local data, secrets,
logs and one-off worker tasks, checks known credential patterns and refuses
linked files. Review is still required. Build and test that frozen snapshot;
record its source digest, exact image ID, test results and outstanding gates.
This local working-tree candidate is not a Git-tagged release and must not be
represented as the contents of its `SOURCE_BASE_COMMIT`. Publication still
requires the reviewed commit and CI procedure below.

1. Review all changes, including untracked files, before committing them. Preserve
   local `.env`, databases, attachments and agent logs outside Git. Never use an
   unreviewed `git add .` as a release step. Check the staged file list for secrets.
2. Merge the accepted changes through a pull request. CI runs on PRs and pushes
   to `staging` and `main`; a branch must actually exist before it can be used.
3. On the final commit, manually run the `CI` workflow (`workflow_dispatch`).
   The release-candidate job depends on Windows/Linux tests, PostgreSQL tests,
   the actual production image checks, dependency audit, deployment checks and
   the bilingual landing build.
4. Download `spooldesk-release-candidate-<commit>`. It contains the source archive
   (ZIP and tar.gz) made by `git archive` from that commit, the **same image**
   that passed Docker startup checks, `SOURCE_COMMIT`, `IMAGE_ID`, `IMAGE_TAG`,
   `INSTALL-RU.md`, `VALIDATION.json` and `SHA256SUMS`.
   The earlier `candidate-image-*` artifact alone does not mean all checks passed.
5. Verify `sha256sum --check SHA256SUMS`. Load the image using
   `docker load --input spooldesk-image.tar.gz`; its local tag is
   `spooldesk:rc-<commit>`. Check `docker image inspect` against `IMAGE_ID`.
   `IMAGE_ID` identifies a local image; it is not a registry manifest digest.

The workflow only creates temporary GitHub Actions artifacts. It does **not**
create a Git tag, publish a release, push an image or deploy a website/app.
Artifact expiry is not permanent release hosting. Public source archives must
still be inspected: `git archive` excludes untracked files, not secrets that
someone accidentally committed.

## Transfer and clean installation

Follow [SECOND_COMPUTER_INSTALL.md](SECOND_COMPUTER_INSTALL.md). The owner chose
a second computer for the first installation test; no separate VPS purchase is
required. Keep the project name stable and use empty, separate storage.
After loading the accepted image, persist its tag as `SPOOLDESK_IMAGE` in `.env`
and use `--no-build`; rebuilding would produce a different artifact.
Both local and CI candidates include `IMAGE_TAG`; CI uses
`spooldesk:rc-<commit>`. Preserve LICENSE in source and the runtime image.

The owner reports the manual functional tests are done. Record that provenance;
repeat affected flows when code/configuration changes, not the whole manual
suite by default. The second-computer installation has not yet been accepted.
Commercial service readiness is a separate gate in [LEGAL_READINESS.md](LEGAL_READINESS.md).

## Acceptance before publication

- Fresh installation from the downloaded source/image on empty, separate
  storage, using generated test credentials and the documented Python/Docker
  versions. Record the exact commit, image ID/digest and result.
- Run preflight on the intended Compose overlays. Create the first operator,
  log in, handle a website-widget conversation and download an attachment.
- For every channel promised in that release, verify incoming/outgoing messages,
  attachments, retries and threading with dedicated provider test accounts.
  Simulated network tests do not prove SMTP/IMAP or Slack configuration works.
- Exercise a backup and restore into **new** storage with the original separate
  encryption key, then verify authentication, attachments and restart. Review
  quarantined outbound jobs before allowing sending. Follow
  [BACKUP_RESTORE.md](BACKUP_RESTORE.md), including Docker ownership.
- If upgrading an existing installation, back up both DB and attachments before
  migrations. Prove the previous version works with the restored **pre-upgrade**
  backup. Do not assume an older image can read a newer schema. Keep the old
  instance stopped: two active copies can send duplicate messages.
- Record known limits, upgrade/rollback steps and checks in the release notes.
  An independent restore drill does not by itself prove every upgrade path.

For a user-authorized installation trial, publish the reviewed commit and
passed-CI artifacts as a **prerelease**, leaving second-computer acceptance
explicitly pending. Preserve repository visibility; publishing a prerelease
does not authorize changing a private repository to public. Download and verify
the uploaded checksums, commit and image identity before handing it over.

Only after the remaining acceptance gates pass assign a stable version and
publish it as a generally available release.
If an image goes to a registry, record the registry digest and verify the pulled
artifact. For automated client installs pin that digest; do not auto-update
customers to a moving `main` or `latest`.

## First client handover

Agree the client-owned infrastructure, domain, enabled channels, backup location,
who holds the encryption key, who receives alerts and who applies updates.
An external availability check must run outside the app's machine. Define the
maintenance scope and response hours before charging for it. Record a tested
restore and a short operator walkthrough; keep credentials out of handover notes.

The public website is a separate static artifact; see [LANDING.md](LANDING.md).
It can go live before the helpdesk is sold as a generally available release,
provided its offer continues to state the readiness and agreed-scope limits.
