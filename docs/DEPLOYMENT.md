# Deploy Spooldesk on your own infrastructure

One installation serves one organization. Docker Compose starts the dashboard
(including enabled channel workers) and PostgreSQL 16. The public landing is a
separate static site. A laptop is sufficient for a local installation test;
public operation needs a machine with suitable availability, storage and HTTPS.
The software vendor does not need to host that customer's installation.

For a fresh installation on Windows, macOS or Linux, use the
[installation wizard](INSTALLATION.md) ([Russian](INSTALLATION_RU.md)).
It requires Docker only. The commands below are for source builds and server administration.

## Install a reviewed version

Use the source and/or image of an accepted release. Requirements: Docker with
Linux containers, Compose v2 and Python 3.12+ for the setup/backup tools. Do not
copy a development `.env`, preview database or demo credentials.

```sh
python scripts/setup.py
docker compose -p spooldesk up -d --build dashboard
docker compose -p spooldesk ps
```

Open `http://localhost:8000`. The initial owner is `admin@local`; the setup wizard
prints its generated password. Save that password securely. Create individual
operator accounts and configure channels on the owner-only **Integrations** page.
AI is optional and has separate settings. Telegram and a standalone bot process
are not required. The dashboard manages channel workers itself.

For a prebuilt candidate: verify checksums, `docker load --input <image.tar.gz>`,
compare its ID with the config/manifest pair in `package/IMAGE_IDS`
(or `package/IMAGE_IDS_ARM64`), check its platform, and set `SPOOLDESK_IMAGE`
in `.env` to the matching `package/IMAGE_TAG` value. The wizard does this automatically.
Pull `postgres:16-alpine` separately, then start with:

```sh
docker compose -p spooldesk up -d --no-build --pull never dashboard
```

Do not rebuild when accepting a supplied binary image. Do not silently retag
an unknown image as the tested release. Use the same project name for all commands;
changing it creates a different set of volumes and can look like a lost database.

After switching a wizard installation to a custom deployment or editing its
configuration, manage it with Compose. The original launcher intentionally
refuses modified configuration; it is not an upgrade tool.

## Daily operation and storage

```sh
docker compose -p spooldesk ps
docker compose -p spooldesk logs --tail 100 dashboard
docker compose -p spooldesk stop
docker compose -p spooldesk start
```

`pgdata` holds PostgreSQL, `data` holds local attachments and fallback key
material, and `logs` holds application logs. The database port is private;
dashboard port 8000 binds to loopback. **Do not use `down -v` on retained data.**
Do not rerun setup or change database/encryption secrets as a restart procedure.
Once accounts exist, editing `ACCESS_PASSWORD` does not reset their passwords.

## Public HTTPS deployment

Choose a customer-controlled hostname, point DNS to the machine and make ports
80/443 reachable. Add `SPOOLDESK_DOMAIN=help.example.com` to the private `.env`.
The production overlay runs Caddy, enables secure cookies and sets the trusted
proxy count to one for the direct client → Caddy → dashboard path.

```sh
# From source:
docker compose -p spooldesk -f docker-compose.yml -f docker-compose.prod.yml up -d --build caddy
# For a loaded, pinned application image: omit --build, use --no-build.
```

Starting `caddy` starts its dashboard/database dependencies. The application
remains reachable on local loopback for diagnostics; customers use HTTPS.
If another proxy is introduced, configure `TRUSTED_PROXY_HOPS` for the actual
trusted path. A forged forwarded header is not evidence of a client address.
For an existing nginx/reverse proxy, keep the app private, enable secure cookies,
forward the required headers and validate the effective proxy trust settings.

Preflight runs offline and never proves DNS, TLS or provider delivery:

Run preflight from the source checkout (install its locked Python requirements
in a virtual environment if needed); the runtime image does not include these
maintenance scripts:

```sh
python scripts/preflight.py --env-file .env --public-url https://help.example.com --compose-file docker-compose.yml --compose-file docker-compose.prod.yml
```

Do not paste resolved Compose environments into support chats: they contain secrets.
For local-only checks, use `--local --public-url http://localhost:8000`.

## Backup, restore and upgrades

Use the PostgreSQL procedure in [POSTGRESQL.md](POSTGRESQL.md). Preserve the
original `SECRETS_KEY` separately: it decrypts stored credentials, private AI
history and uploaded document excerpts. A replacement key cannot recover them.
Backups include sensitive conversations and files; restrict and encrypt offsite
storage. A copy on the same disk does not protect against loss of that machine.

Run the backup tool with the **same Compose project and overlays** as deployment:

```sh
python scripts/postgres_backup.py --help
```

For example set `COMPOSE_PROJECT_NAME=spooldesk` in the shell (PowerShell:
`$env:COMPOSE_PROJECT_NAME='spooldesk'`) and the actual `COMPOSE_FILE` overlays,
then run `python scripts/postgres_backup.py backup --destination ./backups`.
The tool stops its known writers, dumps PostgreSQL and files, verifies the
archive and restarts the previously running writers. Stop any extra writers.
No scheduler or offsite storage is installed automatically.

Restore into new isolated storage first, with the original separate key and the
matching release. Recovery quarantines pending deliveries: review them against
provider history before retrying. Never start two recovered copies against the
same live channels. Rehearse restore and assign a responsible person.

Before upgrading, back up DB/files and keep the previous image. Install an
explicit reviewed version, not an unqualified `git pull` or moving `latest`.
Startup applies ordered transactional schema revisions. Rollback uses the
pre-upgrade backup and matching application; an older image may not read the
new schema. See [backup and restore](BACKUP_RESTORE.md).

## Channels

Configure connections in **Integrations**, save, then check authentication and
complete a real incoming → ticket → reply → customer receipt test. A saved
secret or HTTP health response is not delivery evidence. Details and diagnostics:
[integration guide](INTEGRATIONS.md).

- **Email:** an existing mailbox, IMAP/SMTP hosts and app password. Defaults are
  IMAP 993 and SMTP 587 STARTTLS; SMTP 465 uses implicit TLS. Check incoming and
  outgoing access independently. The mailbox/domain must already work with its
  provider. Threading uses stored message headers where available. Delivered
  means provider acceptance, not proof of inbox placement or reading.
- **Slack:** bot token, signing secret and public HTTPS origin. Disable Socket
  Mode for this HTTP adapter. Configure Events `/slack/events`, `message.im`
  and applicable channel events; configure `/slack/interactive` for interactive
  controls. Enable messages in App Home, install/reinstall the app after scope
  changes and invite it to required channels. `chat:write` and history scopes
  support messaging; files/names additionally need `files:write`, `files:read`,
  `users:read`. A browser GET to `/slack/events` returning 405 is expected.
- **Telegram:** optional bot token for customer intake; operators answer in the
  dashboard. Do not configure an admin group as a required user workflow or
  promise legacy group reply buttons. Only one polling installation per bot.
- **Website:** enable the widget, set its public origin and allowed website
  origins, then embed the generated script. The allowlist controls browser
  embedding, not arbitrary non-browser clients. Verify on the actual site.

Local attachments from mail, widget and operator uploads use `ATTACHMENTS_DIR`;
some provider-side files remain dependent on that provider. Back up the content
store consistently with PostgreSQL. Edits/deletion/thread behaviour varies by
channel; email cannot guarantee recall of a delivered message.

### Telling the widget who someone is

If your site already knows who is logged in, hand that over and the operator
sees a person instead of a session id:

```html
<script>
  Spooldesk.identify({
    name: "Ann Fowler",
    email: "ann@example.com",
    userId: "cust_7",   // your id for them
    hash: "…"           // see below
  });
</script>
```

Also available: `Spooldesk.open()`, `Spooldesk.close()`, and
`Spooldesk.reset()` — call `reset()` when someone logs out, or the next
person at that computer inherits their conversation.

### Proving it

Anything a page says about a visitor is something the visitor can change in
devtools. That matters the day someone writes in as your billing manager and
asks an operator to move an account.

So set an **Identity secret** in Integrations, and compute `hash` **on your
server** — never in the browser, which would publish the secret to everyone:

```python
import hashlib, hmac

hash = hmac.new(SPOOLDESK_SECRET.encode(), customer_id.encode(),
                hashlib.sha256).hexdigest()
```

```javascript
const hash = require("crypto")
  .createHmac("sha256", process.env.SPOOLDESK_SECRET)
  .update(customerId)
  .digest("hex");
```

Sign the `userId` if you send one, otherwise the lowercased email — whichever
identifies the person. The name is not signed, so you can correct a typo in
someone's name without reissuing anything.

Tickets then say **Verified identity**. Without a signature they say **Says
they are this person**, which is the honest label and is perfectly fine for
most support — it is a reason to check before resetting an account, not a
reason to distrust the visitor.


## AI, knowledge and public help

Provider, model and key are selected in AI settings. Use the matching provider:
an OpenRouter key belongs to OpenRouter, not the direct Anthropic endpoint.
Providers include Gemini, Anthropic, OpenAI, OpenRouter and configured Ollama.
API fees and provider terms are separate. No fixed model availability, quota or
pricing promise is made. Ollama's data path depends on the endpoint you configure.

Upload company instructions and maintain knowledge articles; this provides
context, not model fine-tuning. The assistant uses the current operator's rights,
bounded ticket-content samples, private chat history and TXT/MD/CSV/JSON/LOG/DOCX
attachments. Status/assignee changes require explicit confirmation. See
[AI_ASSISTANT.md](AI_ASSISTANT.md) for limits and data handling.

Published help articles may be accessible publicly and through the widget.
Do not put internal instructions, customer details or secrets into published
articles. Drafts remain unpublished; review publication separately from AI context.

## Health and troubleshooting

Monitor `/healthz` from outside the application machine. Docker marks failed
healthchecks unhealthy; `restart: unless-stopped` restarts exited processes,
not every unhealthy container. A switched-off laptop cannot send its own alert.

- Startup/500: inspect dashboard logs; preserve storage and the original key.
  Do not reset a customer DB to clear an error.
- Wrong login or apparently different data: confirm Compose project, volume,
  port and image. Do not regenerate credentials or seed data.
- Slack URL verification: check HTTPS reachability, HTTP transport configuration
  and generated callback URLs. `127.0.0.1` cannot receive Slack's external events.
- Mail receipt without reply: inspect outgoing delivery and SMTP check separately,
  then mailbox thread/spam. An uncertain delivery must not be blindly resent.
- AI 401: verify the selected provider and its key; changing model cannot fix
  authentication. Never send the key in screenshots or issue reports.

## Direct Python installation

Provision PostgreSQL separately. Use Python 3.12 and the locked dependencies:

```sh
python -m venv .venv
# Activate it using the command appropriate to your OS.
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
python scripts/setup.py
python -m uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Set split PostgreSQL credentials for that server; clear `POSTGRES_HOST` if using
an explicitly encoded `DATABASE_URL` instead. Manage process supervision, TLS,
storage permissions and backups yourself. Docker is the recommended first path.
SQLite is retained for isolated tests/legacy recovery, not a new production install.
