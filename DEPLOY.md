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

Needs **Python 3.13**. Not 3.14: some dependencies have no wheels for it yet
and would fall back to a fragile source build.

```bash
# 1. Virtualenv and dependencies
python3.13 -m venv .venv
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
> installs it explicitly because `python:3.13-slim` ships without it.

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
| `BOT_TOKEN` | yes | Bot token from @BotFather |
| `ADMIN_CHAT_ID` | yes | Id of the chat tickets are posted to |
| `ACCESS_PASSWORD` | yes | Password for the first sign-in (`admin@local`) |
| `DATABASE_URL` | yes | Database URL — Docker sets this itself |
| `DASH_SECRET_KEY` | no | Signs session cookies; generated and stored in the database if unset |
| `DASH_COOKIE_SECURE` | no | `true` when serving over HTTPS |
| `GEMINI_API_KEY` | no | Turns the AI suggestions on |
| `SLACK_BOT_TOKEN` | no | Slack channel — needs `SLACK_SIGNING_SECRET` too |
| `SLACK_SIGNING_SECRET` | no | Proves an incoming event really came from Slack |

The full list with comments is in [`.env.example`](.env.example).

---

## Slack

Optional. Lets a client write to you in Slack — a direct message, or a
channel shared with their whole team — and have it arrive as an ordinary
ticket. Off until both variables above are set, because a token without a
signing secret can send but cannot safely receive: the instance would work
right up until somebody forged an event at it.

1. Create an app at <https://api.slack.com/apps> → **From scratch**.
2. **OAuth & Permissions** → bot token scopes:
   `chat:write`, `im:history`, `channels:history`, `groups:history`.
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
covers the conversation, and each message carries who wrote it. Attachments
are not carried in either direction yet, and the operator's reply box says so
rather than failing after the click.

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
