# Using Spooldesk

This is the day-to-day guide: what an operator does, and what the product
does on its own. [DEPLOY.md](DEPLOY.md) covers getting it running;
[FAQ.md](FAQ.md) covers the questions people ask before they install it.

---

## What a ticket is

One conversation with one customer, on whichever channel they wrote from.
The widget, Slack and Telegram all arrive in the same queue and are answered
the same way — the channel is a fact about a ticket, not a separate inbox to
check.

A customer has **one active ticket at a time**. Writing again continues it
rather than opening a second one; a new ticket starts once the old one is
closed. That is deliberate: five parallel threads from one person is not a
feature to whoever has to answer them.

---

## The board

Four columns. Three of them are stages you set; the fourth is derived.

| Column | What it means |
|---|---|
| **New** | Nobody has replied yet |
| **In progress** | Somebody is working on it |
| **Waiting** | The ball is with the customer |
| **Closed** | Finished — last 7 days only |

**New → In progress happens by itself** the moment you reply. It never
downgrades a stage you set on purpose, so moving a ticket to Waiting and then
replying to something else does not undo your decision.

**Waiting is the one that pays for itself.** The response clock only runs
while the customer is waiting on *you*. Park a ticket in Waiting and the
clock stops; it starts again when they answer. A card that has been in
Waiting for a week is not late — it is a customer who has gone quiet, which
is a different problem and looks different on the board.

The clock also counts **working hours**, configured in Settings. A message at
18:05 is not overdue at 09:00 the next morning. Amber past the response
target, red past twice it, and nothing at all when nothing is owed.

Closed is capped at seven days and every column at fifty cards. The board is
for triage; history lives in **Tickets**, which has filters and Excel export.

---

## Answering

Open a ticket and you get the whole conversation, whichever channels it
crossed, in order.

**Reply** goes to the customer. **Note** does not — internal notes are
operator-only, never sent, and never given to the AI as context. That last
part matters: a note saying "this is the third time this week" should not
come back as a suggested reply.

**Attachments work both ways on every channel.** Drag a file onto the reply
box, or paste a screenshot straight into it. Images from customers appear
inline; anything else is a download. Widget visitors can do the same.

**Templates** are canned replies, scoped to a team, with shortcuts. The AI
picks likely candidates for you when it is switched on; you still choose.

**A signature** can be appended to everything you send — Preferences →
Signature. Empty by default, because a signature nobody chose is a change to
what customers receive.

### Closing

Pick one or more close reasons, and add free text if none of them fit.
Reasons are editable in Settings, so they should end up describing what
actually happens in your support rather than what the defaults guessed.

Closing tells the customer, on whatever channel they wrote from, and asks
them to rate it 1–5. The score is immutable and shows up on the dashboard;
nobody is asked twice.

The rating is **answerable in Telegram today**. Widget and Slack customers
get the closing notice and see the question, but the panel has no buttons to
tap yet — so their scores are missing from the CSAT figure rather than being
zero. Worth knowing before reading that number as the whole picture.

---

## Working faster

**Saved views** are a set of filters with a name — "my unassigned", "billing
this week". They live in the Tickets page and are per operator.

**Preferences** (`g` then `p`) is per operator, not per instance. Nothing you
change there affects a colleague.

- **Language** — the dashboard's, independent of what customers see.
- **Theme** and **density** — dark, light, or follow the system.
- **Landing page** — where you start. Board people and queue people want
  different answers.
- **Advance to the next ticket after replying** — off by default. It moves
  the page out from under you, which is welcome when you asked for it and
  alarming when you did not.
- **Signature**, **hotkeys**, **notify on new**.

### Keyboard

Hotkeys can be switched off in Preferences. Anywhere:

| Keys | |
|---|---|
| `g` then `t` | Tickets |
| `g` then `b` | Board |
| `g` then `p` | Preferences |
| `/` | Search |
| `?` | This list |
| `Esc` | Close whatever is open |

In a ticket:

| Keys | |
|---|---|
| `r` | Reply |
| `n` | Internal note |
| `Ctrl` + `Enter` | Send |
| `j` | Next ticket |
| `Esc` | Back |

**Search** covers ticket id, message text, the author's username and the
category, and folds case in Unicode rather than only in ASCII — searching
`okonomie` for `Okonomie` works in any database, but searching `ökonomie`
finds `Ökonomie` here and would not in a plain SQL `LIKE`. The same is true
of every non-Latin alphabet.

---

## Answering from Telegram

If the Telegram channel is set up, every ticket also lands in your admin
group — including tickets that arrived from the widget or Slack. Replying in
the thread reaches the customer on their own channel.

This is for answering away from a desk. Everything else — the board, close
reasons, templates, notes, reports, settings — is the dashboard.

---

## Who the customer is

Telegram gives you an account. Slack gives you a workspace user. The widget
gives you whatever the visitor's page said, and that is worth reading
carefully.

A ticket shows either **Verified identity** or **Says they are this person**.
The second is the normal case and most of those people are exactly who they
say — a widget runs on a page the visitor controls, so an unsigned name is a
claim, not a fact. Treat it as a reason to check another way before doing
something irreversible: resetting access, discussing an account, quoting a
balance.

Verified means the customer's own server signed the identity with a shared
secret. See "Proving it" in [DEPLOY.md](DEPLOY.md) for how a site sets that
up.

---

## What the AI does, when it is on

It is off unless a `GEMINI_API_KEY` is set. When on, it suggests — it never
sends.

- Searches previous resolved tickets and drafts an answer for a new one.
- Picks likely templates.
- Every suggestion is Accept/Dismiss, and the accept rate, latency and token
  cost are measured rather than assumed. They are on the AI analytics page,
  so "is this worth paying for" has an answer.

Internal notes are excluded from everything it sees. So are sensitive
categories, whose message bodies are masked on the ticket page as well.

---

## Reports

**Dashboard** is the shape of the week: ticket trend, first-response and
resolution times, CSAT, category split, an activity heatmap by hour.

**Reports** is the detail, with date ranges and export.

Both count what happened, not what was configured. A category nobody uses
shows as a category nobody uses.
