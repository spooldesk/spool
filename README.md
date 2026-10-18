# Spooldesk

A self-hosted helpdesk for customer conversations across email, website chat,
Slack and optional Telegram. Operators work in one web dashboard.

**Release model:** one organization per installation, on infrastructure you
control. No Spooldesk cloud account, mandatory subscription or maintainer-owned
VPS is required. Source is available under [BSL 1.1](LICENSE), not an OSI
open-source licence. Paid installation and maintenance are separate services.

## Install

### Download and install

**[↓ Download Spooldesk v0.1.0-rc.9 — complete installation package](https://github.com/spooldesk/spool/releases/download/v0.1.0-rc.9/spooldesk-v0.1.0-rc.9-installer.zip)**

Extract the entire ZIP into a permanent folder and open one file:

| OS | Launcher |
| --- | --- |
| Windows | `INSTALL-WINDOWS.cmd` |
| macOS Intel / Apple Silicon | `INSTALL-MACOS.command` |
| Linux x86-64 / ARM64 | `INSTALL-LINUX.sh` (Run in Terminal) |

The wizard checks files and Docker, selects the image, asks for a password and
port, starts PostgreSQL and Spooldesk, then opens the dashboard. Repeat the same
launcher from the same folder to start the existing installation.

**Only Docker is required:** Docker Desktop with Linux containers on Windows/macOS,
or Docker Engine with Compose v2 on Linux. No host Python or Git is needed.
Internet is required for the first PostgreSQL download. This is a terminal wizard,
not a signed native installer; macOS/Linux may ask you to allow execution.

[Installation guide](docs/INSTALLATION.md) ·
[Russian guide](docs/INSTALLATION_RU.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md) ·
[ZIP checksum](https://github.com/spooldesk/spool/releases/download/v0.1.0-rc.9/spooldesk-v0.1.0-rc.9-installer.zip.sha256) ·
[Release notes](https://github.com/spooldesk/spool/releases/tag/v0.1.0-rc.9)

This is a prerelease, with manual installation acceptance still pending.
Rc.9 improves confirmation, cancellation and input validation, adds customer
installation guides and keeps strict archive/image/platform verification.
**Private preview:** sign in with a GitHub account that has repository access.
The direct download does not grant access to an otherwise unauthorized account.

**Security update (2026-10-13):** `rc.9` retains `multidict 6.9.1`, fixing
[CVE-2026-104874](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925),
and the maintenance tool lock pins `uv 0.12.18`. Use this candidate for a new
installation; the older release assets have not been replaced.

### Download from a terminal

With [GitHub CLI](https://cli.github.com/) installed, sign in once using
`gh auth login`, then run this command in PowerShell, Bash or another terminal:

```sh
gh release download v0.1.0-rc.9 --repo spooldesk/spool --pattern 'spooldesk-v0.1.0-rc.9-installer.zip*' --dir spooldesk-download
```

This downloads the same ZIP and its SHA-256 file. It does not start an installer
or modify an existing deployment. After extraction, open the launcher for your OS. See `INSTALLATION.md` for prerequisites
and troubleshooting; the wizard checks package integrity automatically.

<details>
<summary>Build from source instead</summary>

The [installation wizard](docs/INSTALLATION.md) needs only Docker.
Building from source additionally requires Python 3.12+ for setup.

Extract a versioned source archive into a new directory, then run:

```sh
python scripts/setup.py
docker compose -p spooldesk up -d --build dashboard
docker compose -p spooldesk ps
```

Open `http://127.0.0.1:8000`. Sign in as `admin@local` with the password you
chose or generated during setup. This is a fresh PostgreSQL installation.
Configure channels in the owner's **Integrations** tab.
The dashboard process runs the channel workers; a separate `bot` service is
available for legacy deployments but is not needed for this first installation.

The local address is only for testing on that computer. For a public installation,
use HTTPS and the production configuration in [deployment guide](docs/DEPLOYMENT.md).
Prebuilt images must be selected by their supplied versioned tag using
`SPOOLDESK_IMAGE`; see the installation guide. Do not rebuild when testing the
prebuilt release artifact.

</details>

## What is included

| Area | Available workflow |
| --- | --- |
| Tickets | Active/closed queues, board, assignment, categories, search and exports |
| Conversations | Replies, internal notes, files, channel-aware edits/deletions, colleague typing indicators |
| Customers | Verification and team assignment, channel identities linked to a customer profile |
| Operators | Individual accounts, invitations, built-in and custom section roles |
| Knowledge | Draft/published articles, website help centre, reply templates |
| Analytics | Queue, response/resolution metrics, working-hour SLA and reports |
| Connections | Owner-only configuration, credential checks and delivery diagnostics |
| AI, optional | Ticket-content search/analysis, drafts, private chat history, text/DOCX files and confirmed status/assignee changes |

The AI provider is selected in settings: supported external providers include
OpenRouter; a supported Ollama endpoint can be used locally. Provider costs
and infrastructure are separate. The assistant does not execute arbitrary code.
Channel capabilities differ: an email already delivered cannot be withdrawn
from a recipient's inbox. See [operator guide](docs/USER_GUIDE.md) and [FAQ.md](FAQ.md).

![Ticket board with synthetic data](docs/img/01_board.png)
![Conversation with synthetic data](docs/img/03_ticket_detail.png)

These illustrations use synthetic data. They are examples, not customer
endorsements or evidence of a tested capacity limit. The public landing has a
separate local interactive demo; it does not connect to a real helpdesk.

## Architecture

```mermaid
flowchart LR
    C[Email / Slack / widget / optional Telegram] <--> D[Dashboard and channel workers]
    O[Operators] <--> D
    D <--> P[(PostgreSQL)]
    D <--> F[Local attachment storage]
    D --> A[Optional selected AI provider]
```

Docker uses Python 3.12, FastAPI, SQLAlchemy/asyncpg and PostgreSQL 16.
Incoming and outgoing jobs use database-backed queues and leases. Unknown
delivery is flagged for review rather than blindly resent. SQLite is retained
for isolated tests and legacy recovery, not new production installations.

One installation serves one organization. Teams are routing groups, not SaaS
tenants. Some limits are process-local and write transactions share a database
lock; no horizontal-scaling or load-capacity guarantee is made.

## Release and operations

See [self-hosting](docs/SELF_HOSTING.md) for infrastructure responsibilities,
[release notes](docs/RELEASE_NOTES.md) for changes and current limits, and the
release asset `package/VALIDATION.json` for the exact commit and CI evidence.

Persist PostgreSQL and attachments, keep the encryption key separately, and
practice restoration. Backups and external uptime monitoring remain the
installation owner's responsibility unless explicitly included in a contract.
Connected messaging and AI providers receive the data needed for enabled
features; self-hosting alone does not establish privacy-law compliance.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Installation](docs/INSTALLATION.md) / [Russian](docs/INSTALLATION_RU.md) | Windows, macOS and Linux setup |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Installation errors and safe recovery |
| [Self-hosting](docs/SELF_HOSTING.md) | Availability, security and maintenance responsibilities |
| [Deployment](docs/DEPLOYMENT.md) | Docker, TLS, configuration, channel setup and troubleshooting |
| [Operator guide](docs/USER_GUIDE.md) | Daily ticket and customer workflows |
| [FAQ](FAQ.md) | Requirements, licensing and current limits |
| [Connections](docs/INTEGRATIONS.md) | What integration diagnostics do and do not prove |
| [AI assistant](docs/AI_ASSISTANT.md) | Permissions, context, files and chat history |
| [Customer AI agent](docs/AI_CUSTOMER_AGENT.md) | Automatic conversations, limits, handoff and delivery diagnostics |
| [PostgreSQL](docs/POSTGRESQL.md) | Database setup and migration policy |
| [Backup and restore](docs/BACKUP_RESTORE.md) | Recovery and outbound quarantine |
| [Landing](docs/LANDING.md) | Bilingual site sources and public-only export |
| [Security](SECURITY.md) | Reporting issues and deployment boundaries |

Date-stamped audits in `docs/internal/archive/` are historical snapshots, not current product
instructions. [Local development](https://github.com/spooldesk/spool/blob/main/docs/internal/LOCAL_DEVELOPMENT.md) describes this repository's
developer preview only; never use its demo credentials for a customer install.

## Development

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-dev.txt
python -m pytest -q
node --test tests/frontend/*.test.cjs
```

Tests use disposable databases and fake providers. The supported container
runtime is Python 3.12. See [CONTRIBUTING.md](CONTRIBUTING.md); do not use a real
customer database or live messaging for tests.

## Licence and contact

The current BSL Additional Use Grant permits own production use within its
terms. Providing third parties a hosted or managed service with a substantial
set of the product's functionality requires separate rights. Installer/MSP
arrangements need assessment against the actual licence; there is no blanket
reseller permission. The licence specifies its change date and change licence.
Optional paid services do not remove rights already granted for a version.

Project author and licensor: **Kostiantyn Boiko**.
Product and licensing enquiries: [hello@spooldesk.net](mailto:hello@spooldesk.net).
Issues and contributions: [contribution guide](CONTRIBUTING.md).
