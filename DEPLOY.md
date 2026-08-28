# Deployment

Running the support bot and the dashboard on your own server. Two supported
routes: **Docker** (recommended) and **by hand**.

---

## What you need first

1. **A bot token** — create a bot with [@BotFather](https://t.me/BotFather)
   (`/newbot`) and copy the token, which looks like `123456:ABC-DEF…`.
2. **The admin chat id** — the group the bot posts tickets into. Forward any
   message from that group to [@getmyid_bot](https://t.me/getmyid_bot) to find
   it. Group ids are negative, e.g. `-1001234567890`.
3. **A server** with Docker and Docker Compose v2. A small Linux VPS is plenty.

---

## Route 1 — Docker (recommended)

```bash
# 1. Generate .env (asks for the token, chat id and a password; keys are made for you)
python scripts/setup.py
#    or by hand: cp .env.example .env  and fill it in

# 2. Build and start
docker compose up -d --build

# 3. Open the dashboard
#    http://<your-server>:8000
```

That is the whole thing. The bot starts accepting messages and the dashboard
is on port `8000`.

### First sign-in

- **Login:** `admin@local`
- **Password:** whatever `ACCESS_PASSWORD` is set to in `.env` (the setup
  script prints it at the end).

Create personal accounts for your operators on the **Operators** page straight
away, then disable or change `admin@local`. Roles are enforced per route and
the audit trail is only as good as the accounts behind it.

### Day-to-day

```bash
docker compose ps              # status
docker compose logs -f         # everything
docker compose logs -f bot     # just the bot
docker compose restart         # restart
docker compose down            # stop
```

Or shorter, through `make`: `make up`, `make logs`, `make down` — see
`make help`.

### Data and backups

The entire database is one SQLite file on the `data` volume, at
`/app/data/support.db`.

```bash
docker compose cp dashboard:/app/data/support.db ./backup-$(date +%F).db
```

It holds every conversation in plain text. Treat the backup the way you would
treat the conversations.

### Upgrading

```bash
git pull
docker compose up -d --build
```

The schema migrates itself at startup, and does so idempotently — running an
old container against a newer database, or the reverse, does not corrupt it.

---

## Route 2 — by hand, without Docker

Needs **Python 3.12**. Not newer: aiogram pins aiohttp to 3.9, which
publishes no Linux wheel past 3.12, so anything above compiles from source
and would fall back to a fragile source build.

```bash
# 1. Virtualenv and dependencies
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configuration
python scripts/setup.py            # or cp .env.example .env and fill it in

# 3. Start — TWO processes, in separate terminals or under systemd:
python -m src.main                                   # the bot
python -m uvicorn src.dashboard.app:app --port 8000  # the dashboard
```

They share one SQLite file in WAL mode: the bot writes, the dashboard reads and
manages. Either can run without the other — the dashboard needs no bot token
until you actually send something.

> On Windows the `tzdata` package is required for time zones; it is already in
> `requirements.txt`. On Linux it comes from the system, and the Docker image
> installs it explicitly because `python:3.12-slim` ships without it.

---

## HTTPS in production

Dashboard authentication is a signed cookie, so it belongs behind TLS. With a
proxy in front (nginx, Caddy, Cloudflare) set this in `.env`:

```
DASH_COOKIE_SECURE=true
```

An nginx location block:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Do not bind the dashboard to `0.0.0.0` without something in front of it — it
has no IP allowlist of its own.

---

## Environment variables

| Variable | Required | What it does |
|----------|:--------:|--------------|
| `ACCESS_PASSWORD` | yes | Password for the first sign-in (`admin@local`) |
| `DATABASE_URL` | yes | Database URL — Docker sets this itself |
| `BOT_TOKEN` | no | Bot token from @BotFather — needs `ADMIN_CHAT_ID` too |
| `ADMIN_CHAT_ID` | no | Id of the group tickets are posted to (negative) |
| `DASH_SECRET_KEY` | no | Signs session cookies; generated and stored in the database if unset |
| `DASH_COOKIE_SECURE` | no | `true` when serving over HTTPS |
| `ATTACHMENTS_DIR` | no | Where widget attachments are kept (default `./data/attachments`) |
| `GEMINI_API_KEY` | no | Turns the AI suggestions on |
| `SLACK_BOT_TOKEN` | no | Slack channel — needs `SLACK_SIGNING_SECRET` too |
| `SLACK_SIGNING_SECRET` | no | Proves an incoming event really came from Slack |

**Telegram is optional.** With `BOT_TOKEN` and `ADMIN_CHAT_ID` set, tickets
appear as threads in your admin group and operators can answer from Telegram.
Without them the dashboard runs, the website widget takes tickets and everything
else works — only the Telegram channel is absent. Running the widget alone:

```bash
docker compose up -d --build dashboard
```

The `bot` service is Telegram; started without a token it says which variable
is missing and exits, rather than failing later on an authentication error.

The full list with comments is in [`.env.example`](.env.example).

---

## Website widget

A chat bubble on your own site. Off until you switch it on, because it is
the one part of this product a stranger can reach: **Settings → Website
widget → Enable**, then paste the line it shows you before `</body>` on
every page that should have the bubble.

Two settings are worth understanding before you turn it on.

**Sites allowed to embed it.** One origin per line. Empty means any site.
This is a browser rule, not a lock — anything that is not a browser ignores
it — so it stops a competitor pasting your widget on their page, and does
not stop a script. What stands in the way of a script is the rate limiting,
which is always on.

**Ask for a name and email first.** A two-field form in front of the first
message. Off by default: every box before a support request is a support
request that does not get sent. Turn it on if you answer by email, leave it
off if you answer in the chat.

### Attachments

Both directions, up to 10 MB per file. Visitors can click the paperclip,
drag a file onto the panel, or paste a screenshot straight into the box —
which is how most support attachments actually arrive. Images appear inline
in the conversation and on the ticket page; everything else is a download.

This is the one channel that stores its own files. Telegram hands back a
`file_id` and keeps the bytes; a browser cannot, so they go to
`ATTACHMENTS_DIR` — by default `./data/attachments`, which in Docker is on
the same volume as the database. **Back it up with the database, not
separately.** They are two halves of one record: a restored database whose
attachments are missing is a helpdesk full of dead paperclips.

Nothing is served under the Content-Type the uploader claimed, and files are
fetched with the visitor's session token rather than from a guessable URL —
so an attachment is only ever readable by the conversation it belongs to.

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

So set an **Identity secret** in Settings, and compute `hash` **on your
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

---

## Slack

Optional. Lets a client write to you in Slack — a direct message, or a
channel shared with their whole team — and have it arrive as an ordinary
ticket. Off until both variables above are set, because a token without a
signing secret can send but cannot safely receive: the instance would work
right up until somebody forged an event at it.

1. Create an app at <https://api.slack.com/apps> → **From scratch**.
2. **OAuth & Permissions** → bot token scopes:
   `chat:write`, `im:history`, `channels:history`, `groups:history`,
   and — for attachments and names — `files:write`, `files:read`,
   `users:read`.
3. Install it to the workspace and copy the bot token (`xoxb-…`).
4. **Basic Information** → copy the **Signing Secret**.
5. Put both in the environment and restart.
6. **Event Subscriptions** → request URL `https://your-host/slack/events`.
   Slack verifies it immediately, so the instance has to be running and
   reachable first. Then subscribe the bot to `message.im` and
   `message.channels`.
7. Invite the app into whichever channels it should watch.

The request URL is also shown on the Settings page once the credentials are
in place, filled in with the host that served the page — that is the one to
copy, because the commonest setup failure is pointing Slack at the wrong
host.

**What arrives where.** A direct message is one person, so they become the
customer. A shared channel is a room: the room is the customer, one ticket
covers the conversation, and each message carries who wrote it. Files travel
both ways: what a client attaches opens from the ticket page, and what an
operator attaches is uploaded to the conversation.

**Three scopes are optional in practice.** Without `files:write` the app
cannot send attachments — the first refusal is enough for the instance to
stop offering the button, rather than failing again for every operator in
turn. Without `files:read` a client's attachment cannot be fetched back for
the ticket page. Without `users:read` each message is labelled with a Slack
user id instead of a name: not pretty, but it still tells two people in a
shared channel apart, which is what the label is for.

**Credentials stay out of the dashboard on purpose.** The Settings page shows
whether Slack is connected, never what its secrets are — a signing secret on
a screen is a signing secret in a screenshot.

---

## When something is wrong

- **The bot does not answer** → check `BOT_TOKEN`, then
  `docker compose logs -f bot`. If a webhook was set previously, the bot
  removes it on startup by itself.
- **Tickets do not reach the group** → usually a wrong `ADMIN_CHAT_ID` (group
  ids are negative), or the bot is not an administrator of that group.
- **Slack says the request URL is invalid** → the instance must be
  running and reachable from the internet when you paste it; Slack
  verifies it on the spot. Check the host in the URL matches the one on
  the Settings page.
- **Slack messages do not become tickets** → the app has to be *in* the
  channel, and subscribed to `message.channels` (or `message.im` for
  direct messages). Its own messages are ignored by design.
- **Dashboard returns 500 at startup** → most often a damaged or very old
  `support.db`. Back it up and recreate, or read the dashboard log first.
- **`No time zone found`** → `tzdata` is missing. Only happens on a manual
  Windows run: `pip install tzdata`.
