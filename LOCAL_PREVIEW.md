# Local preview

Run from the repository directory with Docker Desktop running:

```powershell
docker compose -f docker-compose.preview.yml up -d --build
```

After the first build, use `up -d` without `--build`. In Docker Desktop, start
or stop the **spooldesk-preview** group. It contains three services:

| Service | Purpose | Open |
| --- | --- | --- |
| postgres | PostgreSQL for the preview | No public port |
| dashboard | Operator dashboard and website chat widget | http://127.0.0.1:8011/tickets |
| landing | Static marketing site, English and Russian | http://127.0.0.1:8765/ru/ |

Sign in with `admin@local` / `admin`. These are public local demo credentials,
not production credentials. On **2026-09-26**, at the user's explicit request,
the canonical volume was reset for manual tests: **zero tickets/customers**, the
same owner login, and the standard **General** team/categories created on startup.
Other operators, demo content, custom roles and connection/AI settings were cleared;
the Russian UI preference was retained. This is still the canonical workspace for
port 8011. Do not restore the former 87-ticket/four-team demo or reseed it.
The pre-reset PostgreSQL backup is in
`.agent-runs/backups/preview-before-clean-test-20260926-143528.dump`.

On **2026-10-01**, the user requested an additive dataset for manual AI testing.
`scripts/seed_ai_preview.py` added **24 customers, 72 tickets (IDs 9–80), 304
messages and five unpublished knowledge articles** with the **AI TEST** label.
There are 48 closed cases and 24 active cases, split evenly between new,
in-progress and waiting for the customer, across the existing three teams.
Six customers await verification/team assignment. The eight pre-existing tickets,
five customers, owner login and all AI/channel settings were preserved.

These are local widget identities with discarded random session tokens, not real
email/Slack/Telegram destinations. Historical responses and CSAT are synthetic.
Knowledge articles remain drafts. No outbound jobs or model calls are created by
the seed. The ID manifest is stored in `app_settings.preview_ai_practice_v1`;
another explicit invocation is a no-op. Never run it automatically at startup.
The before-fill backup is `.agent-runs/backups/preview-before-ai-practice-20261001.dump`.

Try the assistant with: “Найди активные AI TEST обращения про VPN”,
“Подготовь черновик ответа на тикет #57”, or “Предложи перевести #57 в работу”.
Confirm a proposed mutation only when it matches the intended test. Aggregate
reports include this practice data alongside the original channel test cases.

Keep the `spooldesk-preview` project name and its existing PostgreSQL volume.
Do not start old upgrade/recovery previews on port 8011 or substitute another
database when applying code changes. Tests must use separate disposable DBs.

This configuration does not load the application's `.env`, does not use the
deployment volumes, and does not inject live channel credentials. Connections
saved by the owner in the dashboard are still used. Both web ports bind only
to this computer. Do not use it for production or real customer data.

The dashboard starts after PostgreSQL passes its health check. Docker keeps
the processes running independently of Codex and restarts them unless they
were explicitly stopped. Docker Desktop itself must remain running; automatic
Docker Desktop startup after signing into Windows is a separate user setting.

```powershell
# Status and recent dashboard errors
docker compose -f docker-compose.preview.yml ps
docker compose -f docker-compose.preview.yml logs --tail 60 dashboard

# Stop all three services; keep their data
docker compose -f docker-compose.preview.yml stop

# Apply code changes and refresh the browser asset version
docker compose -f docker-compose.preview.yml restart dashboard
```

Restart the dashboard after an update that changes templates together with
Python code, translations or template context. Templates are read directly
from the checkout, but the running Python process retains its earlier code
and translation catalog until restarted. Mixing those revisions can cause
HTTP 500 errors. Restarting the dashboard does not replace its database.

Do not run `down -v` unless you intend to delete the preview database and files.
The deployment configuration remains `docker-compose.yml`; this preview file
is standalone, not an overlay for it. Chatwoot and other Compose projects are
unrelated to this preview.

## Slack tunnel for manual testing

The Slack adapter uses HTTP Events API, not Socket Mode. Opening
`http://127.0.0.1:8011/slack/events` in a browser returns 405 because the route
accepts POST, not GET. Slack needs a publicly reachable HTTPS callback.

With ngrok installed and Docker Desktop running, open PowerShell in this
repository. If ngrok reports `ERR_NGROK_105`, replace the malformed local
authtoken using the token from https://dashboard.ngrok.com/get-started/your-authtoken:

```powershell
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Enter the real token only on your own computer; do not save it in this repo or
send it in chat. Then start the tunnel and leave that terminal open:

```powershell
ngrok http http://127.0.0.1:8011 --traffic-policy-file scripts/slack-tunnel-policy.yml --inspect=false
```

Always include the policy file: it permits only POST to `/slack/events` and
`/slack/interactive`, returning 404 for other requests before they reach the
dashboard. Local request inspection is disabled. Slack signatures are still
checked by Spooldesk. Do not remove the policy if ngrok reports an error.

In Slack app settings, disable Socket Mode. In Event Subscriptions, use the
HTTPS Forwarding address printed by ngrok plus `/slack/events`, wait for
Verified, subscribe to the bot event `message.im`, and save changes. The saved
bot token and Signing Secret in Spooldesk must belong to this Slack app.
For interactive buttons, set Interactivity's URL to the same HTTPS address
plus `/slack/interactive`. Send a new DM, find its ticket in the local dashboard,
and reply there; the reply should arrive in Slack.

In **Integrations → Slack → Connection settings**, also save the tunnel's HTTPS
origin without a path. The page generates both callback URLs and provides an
explicit credential check. This does not set up the tunnel or subscribe Slack
events for you. A successful credential check alone is not proof that inbound
events can reach your computer. See [Connection diagnostics](docs/CONNECTION_CENTER.md)
for the complete test and the meaning of each status.

Stop the tunnel with Ctrl+C. Docker alone does not start it. If its public
address changes, update the Slack callback URLs. The ngrok authentication
attempt on 2026-09-30 failed with ERR_NGROK_105; live forwarding and this policy
still require verification after a valid ngrok token is configured.
