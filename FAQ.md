# FAQ

Questions people ask before installing, answered including the ones where
the honest answer is "not yet".

[DEPLOY.md](DEPLOY.md) is how to run it. [USING.md](USING.md) is how to work
in it.

---

## What is it

**What does Spooldesk actually do?**
Customers write from a chat bubble on your website, from Slack, or from
Telegram. Everything arrives in one queue that your team answers from a web
dashboard: a board, an SLA clock, canned replies, internal notes, reports.

**Is Telegram required?**
No. It started as a Telegram bot and the name still says so in the repository
URL, but an instance can run on the website widget alone. Leave `BOT_TOKEN`
unset and the dashboard runs without it; the startup log says so once rather
than failing later.

**How is this different from a Telegram bot someone wrote in a weekend?**
Mostly in what happens after the first message: the ticket has a stage, a
clock that knows your working hours, an operator it is assigned to, a close
reason, and a searchable history. And in the fact that the customer's channel
is not the operator's — a widget conversation is answered from the same place
as a Telegram one.

**Can I use it for something other than customer support?**
People use ticket queues for internal IT, for order questions, for onboarding
checklists. Nothing here assumes "support" beyond the vocabulary, which is
editable: categories, close reasons and templates are all yours.

---

## Running it

**What does it need?**
A small Linux VPS. Two containers, SQLite, no external services required. It
runs comfortably in well under a gigabyte of RAM — the production compose
file caps the dashboard at 768 MB and the bot at 384 MB, and those are limits
rather than requirements.

**Do I need PostgreSQL?**
Almost certainly not. SQLite in WAL mode runs a support desk with a handful
of operators without complaining, and it makes the whole instance one file to
back up. Two things change that: wanting more than one dashboard process, or
having operations people who would rather administer a database they already
know. PostgreSQL is supported for those — see DEPLOY.md.

**Is there a hosted version?**
Not today. Self-hosting is the product.

**HTTPS?**
The production compose overlay puts Caddy in front and obtains certificates
automatically. One command, one line of `.env`.

**How do I back it up?**
`make backup` writes one archive with the database and its attachments,
taken at the same instant. They are halves of one record: a restored database
without its attachments is a support history full of dead paperclips. Then
copy it somewhere else — the disk holding the only copy is the disk that
fails.

**Can I upgrade without losing data?**
On SQLite, yes: schema changes so far have been additive columns applied in
place at startup. On PostgreSQL there is no migration framework yet, so a
release with a schema change would need Alembic, which is not written. This
is the honest reason to stay on SQLite unless you have a reason not to.

---

## Data and privacy

**Where does customer data live?**
On your server. There is no telemetry, no phone-home, no account to create.

**Does the AI see my tickets?**
Only if you give it a `GEMINI_API_KEY`, and then only what it needs to
suggest an answer. Internal notes are never included. Categories marked
sensitive are masked on the ticket page and excluded entirely. With no key,
no AI code path runs at all.

**Are attachments safe to accept from strangers?**
The widget's upload endpoint is the most exposed thing here and is written
that way: a size cap enforced while reading rather than after, a per-session
rate limit, and files served under a content type from an allowlist rather
than the one the uploader claimed — so an uploaded `.html` cannot come back
as HTML on your own domain. Files are fetched with the visitor's session
token, so an attachment is only readable by the conversation it belongs to.

**Can a visitor pretend to be someone else?**
They can *say* they are anyone — a widget runs on a page they control. The
ticket page therefore distinguishes **Verified identity** from **Says they
are this person**, and verification means the customer's own server signed
the identity with a shared secret. Unsigned claims can fill a blank field but
never overwrite one, so nobody can quietly rewrite the name at the top of a
ticket an operator is already working on.

**GDPR?**
Self-hosted, so you are the controller and the data never leaves your
infrastructure. There is no built-in "export everything about this person"
button yet; tickets are exportable to Excel and the database is yours.

---

## Licensing and money

**Is it free?**
For running your own support, yes, including commercially. Also for deploying
it for a client as part of IT services.

**What is not allowed?**
Reselling it as a hosted or managed service to third parties — that needs a
commercial licence. This is the Business Source License, the same model as
Sentry and MariaDB. The single restriction exists so a cloud provider cannot
take this and outrun a solo maintainer with it.

**Is that open source?**
Not by the OSI definition, and the README says so plainly rather than
fudging it. Full source, no feature gates, no telemetry, no seat limits.
Every release converts to Apache 2.0 four years after publication, so nothing
can be taken away later.

**Can I remove the branding?**
Yes. The brand name shown to customers is a setting, and there are no
"powered by" links to strip.

---

## Limits worth knowing

**How many operators?**
No seat limit in the software. The practical ceiling on SQLite is set by
write concurrency, and a team of a few operators is nowhere near it.

**Is there an API?**
Not yet — webhooks and a REST API are on the roadmap. Today the integration
points are the channels themselves and the database.

**Can one customer have several conversations at once?**
No, by choice. One active ticket per customer; writing again continues it.
Their history is visible to them in the widget and to you in the dashboard.

**Can widget customers rate the support they got?**
Not by tapping, yet. The closing message reaches every channel and carries
the question, but only Telegram can draw the buttons to answer it — so the
CSAT figure is built from Telegram ratings, and widget conversations are
absent from it rather than counted as bad ones.

**Email as a channel?**
Not yet. Widget, Slack and Telegram today.

**What language do customers see?**
Theirs, when the channel tells us — Telegram supplies it and the widget reads
the browser's. When it does not, they get the instance's default language,
which is set in Settings and is worth setting deliberately: it is what every
customer with an unknown language will read. Russian, Ukrainian and English
ship; the operator dashboard's language is a separate setting, and each
operator can override it for themselves.

**Does it work on a phone?**
The dashboard is responsive and the widget is built mobile-first. For
answering on the move, the Telegram admin group is usually nicer than a
browser.
