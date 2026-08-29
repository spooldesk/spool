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
#    http://localhost:8000
```

That is the whole thing. The bot starts accepting messages and the dashboard
is on `8000` — **on loopback only**. On your own machine that changes
nothing. On a server it means the dashboard is not reachable from the
internet until you put TLS in front of it, which is the next section and is
one command.

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
python -m uvicorn src.dashboard.app:app --port 8000 --no-proxy-headers  # the dashboard
```

They share one SQLite file in WAL mode: the bot writes, the dashboard reads and
manages. Either can run without the other — the dashboard needs no bot token
until you actually send something.

> On Windows the `tzdata` package is required for time zones; it is already in
> `requirements.txt`. On Linux it comes from the system, and the Docker image
> installs it explicitly because `python:3.12-slim` ships without it.

---

## On a server, with a domain

Dashboard sign-in is a signed cookie and the widget is embedded in other
people's pages; both need TLS. There is an overlay that adds it.

Point an A record at the server first — Let's Encrypt checks DNS before
issuing, and failed attempts count against a rate limit — then:

```bash
echo "SPOOLDESK_DOMAIN=help.yoursite.com" >> .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

That is it. The certificate is obtained on first start and renewed without
being asked. What the overlay changes:

- **Caddy** on 80 and 443, with automatic HTTPS. Nothing to schedule, nothing
  that expires quietly in ninety days.
- **`DASH_COOKIE_SECURE=true`**, so the session cookie stops travelling over
  plain HTTP.
- **`TRUSTED_PROXY_HOPS=1`**, which is the setting that makes the rate limits
  work. Read the next section before changing it.
- **Log rotation and memory limits**, because the first thing that fills the
  smallest VPS is a log nobody reads.

`make prod-up`, `make prod-logs` and `make prod-down` are the same commands
with less typing.

### Counting the proxies in front of you

`TRUSTED_PROXY_HOPS` has to match reality, and getting it wrong is quiet.

`X-Forwarded-For` is a header anyone can send. Proxies **append** to it, so
behind one proxy it reads `<whatever the client sent>, <what your proxy
saw>` — and only the last entry means anything. Set it to the number of
proxies you actually have:

| In front of Spooldesk | Value |
|---|---|
| Nothing (direct, or local) | leave unset — the header is ignored |
| The overlay's Caddy | `1` (set for you) |
| Cloudflare **and** Caddy | `2` |

Too low and everyone behind the proxy is counted as one visitor, so the
first person to hit a limit locks out the rest. Too high, or set with nothing
in front, and a stranger picks their own identity by sending a header — the
login throttle and the widget's per-address limits then do nothing at all.

### If you already run nginx

Keep it; skip the overlay and use the base compose, which listens on
`127.0.0.1:8000`. Set `DASH_COOKIE_SECURE=true` and `TRUSTED_PROXY_HOPS=1` in
`.env` yourself.

Start uvicorn with **`--no-proxy-headers`** if you run it by hand. Uvicorn
has its own `X-Forwarded-For` handling, on by default, which trusts anything
arriving from `127.0.0.1` — including, on that setup, a client's own forged
header. Two layers reading the same header disagree; the compose files pass
this flag for you.

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    # Replaces rather than appends: nginx's $remote_addr is the only entry
    # that ends up here, which is what TRUSTED_PROXY_HOPS=1 expects.
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 110M;   # operator replies may carry ten files
}
```

Do not publish the dashboard on `0.0.0.0` in front of nothing. It has no IP
allowlist of its own, and the compose file binds it to loopback on purpose.

### PostgreSQL

SQLite is the default, and calling it the default is not an apology. With WAL
it runs a support desk with a handful of operators without complaining, and it
makes the entire instance one file to copy.

Two things change that, and neither is "we got bigger":

- you want **more than one dashboard process** — several instances, a rolling
  deploy;
- your own operations people would rather **administer a database they already
  know**, with their existing backups and monitoring pointed at it.

Then add the overlay, which brings its own server:

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -base64 24)" >> .env
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

It stacks with the TLS one:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               -f docker-compose.postgres.yml up -d --build
```

Pointing at a server you already run instead — set `DATABASE_URL` and skip the
overlay:

```
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/spooldesk
```

The driver ships in the image; there is nothing extra to install.

**Two limitations, stated plainly.**

There is **no migration between the engines**. Starting with PostgreSQL gives
an empty helpdesk. Moving an existing SQLite instance across means moving the
data yourself.

There is **no migration framework** yet. Schema changes have so far been
additive columns applied to SQLite in place, by code that runs at startup and
is skipped on PostgreSQL — where `create_all` builds the current schema
correctly on a fresh database. A PostgreSQL deployment that needs to cross a
release with a schema change will need Alembic, and that is not written.
Sitting on SQLite until you actually need the two things above is a
defensible choice, not a lesser one.

Attachments are unaffected either way: they live on disk, next to where the
SQLite file would have been. Back them up regardless of engine.

### Backups

```bash
make backup      # → ./backups/spooldesk-2026-08-28-1430.tar.gz
```

One archive with the database and its attachments, taken at one instant. The
two are halves of one record: restore a database without its attachments and
every paperclip in every past ticket is dead.

The database is copied with SQLite's online-backup API, not with `cp`. In WAL
mode — which this runs in, so the dashboard can read while the bot writes —
the file on disk is not a complete database by itself, and copying it at the
wrong moment restores into a missing hour.

Copy the archive somewhere else. The disk holding the only copy is the disk
that fails.

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
| `SPOOLDESK_DOMAIN` | no | The domain the production overlay gets a certificate for |
| `TRUSTED_PROXY_HOPS` | no | How many proxies are in front (default 0 = ignore `X-Forwarded-For`) |
| `ATTACHMENTS_DIR` | no | Where widget attachments are kept (default `./data/attachments`) |
| `GEMINI_API_KEY` | no | Turns the AI suggestions on |
| `EMAIL_ADDRESS` | no | Support mailbox — needs the three below too |
| `EMAIL_PASSWORD` | no | Mailbox password, ideally an app password |
| `EMAIL_IMAP_HOST` | no | Where incoming mail is read from |
| `EMAIL_SMTP_HOST` | no | Where replies are sent through |
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

## Telegram

Optional, like everything else here — an instance running only the website
widget needs none of this. What it adds is a place for operators to answer
from their phone, and a copy of every ticket in a chat your team already
watches.

Two values, `BOT_TOKEN` and `ADMIN_CHAT_ID`, and the channel is off unless
both are set. A token with no chat id is an instance that can message
customers but has nowhere to show operators a ticket, which looks configured
and is half-broken; the startup log says so rather than letting you find out.

### The bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`, pick a name and a
   username. Copy the token — `123456:ABC-DEF…` — into `BOT_TOKEN`.
2. Still in BotFather: `/setprivacy` → **Disable**. This is the step that gets
   missed. With privacy mode on, a bot in a group only sees messages that
   start with a command or mention it, so replies typed in a ticket thread
   never reach the helpdesk and the thread looks ignored.

### The group

Create a group, add your bot, and **make it an administrator**. It needs to
post, and — if you want the tidy version below — to manage topics.

**A supergroup with Topics turned on is the arrangement to want.** Each
ticket becomes its own topic: a real thread with a name, that people can mute
individually and scroll back through months later.

An ordinary group also works. The bot falls back to replying to each ticket's
header message, so conversations still hang together, but a busy day is one
long chat rather than a list of threads. Nothing needs configuring to choose
between them — the bot uses whichever it finds itself in.

To turn on Topics: group **Settings → Topics**. Telegram converts the group
to a supergroup when you do, which changes its chat id, so read the id
*after* this step and not before.

### The chat id

Forward any message from the group to [@getmyid_bot](https://t.me/getmyid_bot).
Group ids are negative and supergroup ids start with `-100`, e.g.
`-1001234567890`. Copy it into `ADMIN_CHAT_ID` exactly, minus sign included —
a positive number there is a private chat with a stranger.

### Checking it

Send `/start` to the bot from another account and open a ticket. Within a
second or two it should appear in the group. If nothing arrives:

- **Nothing at all** — the bot is not in the group, or not an admin.
- **Tickets appear, replies do not come back** — privacy mode is still on.
  BotFather → `/setprivacy` → Disable, then **remove the bot from the group
  and add it again**; the setting only takes effect on re-join.
- **`chat not found`** in the log — the chat id is wrong, or the group became
  a supergroup after you copied it.

---

## Email

A mailbox you already have becomes a channel. Nothing is registered with a
third party, no DNS record changes, and no inbound webhook to expose: the
dashboard reads the inbox over IMAP once a minute and replies over SMTP.

Four variables, all required together:

```
EMAIL_ADDRESS=support@yoursite.com
EMAIL_PASSWORD=an-app-password
EMAIL_IMAP_HOST=imap.yoursite.com
EMAIL_SMTP_HOST=smtp.yoursite.com
```

Ports default to 993 for IMAP and 587 for SMTP; set `EMAIL_SMTP_PORT=465` if
your host wants implicit TLS. `EMAIL_LOGIN` covers the hosts whose username
is not the address.

**Use an app password, not the account password.** Gmail and most others
require one for IMAP anyway, and it means the credential sitting in `.env`
can be revoked without changing anybody's login.

### What arrives, and what does not

An email becomes a ticket, its sender becomes a customer identified by their
address, and attachments are kept — a mail server will not hand the same MIME
part over twice, so they are stored the same way the widget's are and
included in the backup archive.

Replies continue the conversation the same way every channel here works: by
who sent them, not by threading headers. So a customer writing again lands on
their open ticket, and a reply to a conversation you closed last month opens
a new one.

**Quoted history is trimmed.** Without it the third message in a thread is
four screens of your own words with "yes, thanks" on top. The trimming is a
heuristic, so the untrimmed original is kept alongside it.

**Machines do not get tickets.** Out-of-office replies, mailing lists,
bounces and copies of your own outgoing mail are all recognised and skipped.
This is not tidiness: without it, an autoresponder and a helpdesk will write
to each other until somebody notices.

### What it cannot do

There is no unsend — a message is on someone else's server the moment it is
accepted, so the delete button is not offered on email tickets rather than
offered and lying. The CSAT rating is spelled into the closing message as
text, because email has no buttons, and cannot be answered by clicking.

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
