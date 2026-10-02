# Operator workflow

Spooldesk operators work in the dashboard for every customer channel.
Telegram remains an optional customer intake channel. Its optional operator
group receives read-only copies; it cannot send replies or mutate tickets.
New copies have no action buttons. Obsolete group buttons explain where to
work and remove their markup when pressed. No Telegram Mini App is required.

## Response time and stages

- New inbound conversation: `new`.
- Confirmed public human reply: `waiting` (waiting for customer).
- Customer replies while waiting: `in_progress`, with a fresh response clock.
- A message arriving after the operator composed a reply still needs an answer,
  even if that reply was only delivered later. This avoids hiding a question
  behind delayed delivery or a retry.
- Failed/pending/uncertain sends, internal notes and automatic acknowledgements
  do not settle the response clock or move a ticket to waiting.
- Manual movement on the board does not erase unanswered customer messages.
  An operator still investigating can keep a ticket in progress.
- Closed tickets are outside response escalation. Total resolution duration
  remains a separate metric; waiting does not erase elapsed history.

The response deadline is measured in configured working minutes: warning at
80%, breach at 100%. The optional escalation returns a breached assigned ticket
to the shared queue. Legacy Telegram SLA alerts are ignored. Board, queue badge
and escalation use the same response-clock expression.

## Personal preferences

Interface language is saved on the operator account at `/me`. There is no global
language editor. Existing accounts without an explicit preference initially use
the installation's legacy default; saving selects an explicit personal language.
Customer message languages at `/manage/settings` are independent of this choice.

Theme and density choices show their checked state immediately and are persisted
with Save. Queue advance opens the next eligible unanswered ticket after a
successful reply. Signature applies only to public replies, not notes.

The new-ticket notification preference and bell share the same account setting.
Desktop alerts also require browser permission and an open dashboard tab.
Hotkeys follow physical keyboard positions, so Russian/Ukrainian layouts work.
Typing fields suppress single-letter shortcuts. Ctrl/Command+Enter sends only
when hotkeys are enabled; Escape exits the field or dismisses help.

## Verification

Use disposable PostgreSQL databases, never the canonical local preview, for
delivery/retry, response-clock, RBAC and language-isolation tests. Fake channel
adapters verify that operator-group messages cannot send customer replies.
Frontend shortcut tests run with `node --test tests/frontend/*.test.cjs`.
