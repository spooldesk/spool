# Workspace AI assistant

Enable **AI settings → AI assistant**, with the main AI switch on and a configured
provider/model. The assistant defaults off. It uses the existing provider API key;
the local Claude development worker is unrelated to customer-facing AI.

The sidebar opens a chat. The **AI** button in the top bar opens that chat with
the current section or ticket as context. Opening it does not call a model:
write a question or choose an example, then press Send. Section analysis is a
bounded snapshot, not an export of every record or the current table filters.

Supported operations:

- Find tickets by text, ID, channel, stage, assignment or missing operator reply.
- Analyze recurring problems from actual public message excerpts, filtered by
  period, team, channel, status or explicit ticket IDs. Each analysis page covers
  at most 60 tickets and reports total matches, coverage and the next offset.
  Search results also contain an opening message, latest customer message and
  latest delivered reply. The detailed ticket reader supplies more conversation.
- Summarize the latest 12 public messages of a ticket and draft a reply.
- Compare ticket counts over two periods and inspect the current workload.
- Read bounded samples from customers, articles, templates, teams, categories,
  closure reasons, operators and roles, subject to section permissions.
- Review safe AI usage/configuration and saved channel-enable flags. Connection
  health must still be tested in Integrations; enable flags do not prove delivery.
- Propose a ticket stage, eligible assignee or closure. One explicit confirmation
  applies one change. Closing queues the normal customer notification and CSAT
  invitation, as disclosed on the confirmation card.

No arbitrary SQL, filesystem access, background autonomous work, bulk changes,
deletion, reopening, configuration changes or free-form messages to customers
are exposed. Reply drafts must be reviewed and sent from the ticket. The model
does not execute code or direct database queries.

## Rights and data

Custom roles have a separate **AI assistant** section. `view` permits chat and
analysis; `edit` also permits proposals and confirmations, provided the operator
has the corresponding ticket/board edit permission. Each data reader checks its
own section permission. Owner-only sections remain owner-only. Team membership
does not introduce a new visibility boundary: this installation uses section
permissions; teams route support work.

The assistant excludes AI-blacklisted categories, deleted messages, internal
notes and original ticket attachment contents. Documents explicitly attached to
the assistant chat are a separate, operator-authorized input. Connection secrets are not supplied to tools.
Obvious credentials in free text are scrubbed, but this is not a general DLP
guarantee. The operator's question, selected data and enabled company reference
are sent to the configured provider. This differs from automatic customer
deflection: an authorized operator may research closed or unassigned tickets.

## Conversation history and files

Conversations now persist in PostgreSQL and can be reopened from **Chat history**,
including after a page reload. The last three completed question/answer excerpts
provide follow-up context; readers still fetch current data for current-state
questions. A new chat starts a separate conversation. Deleting a chat removes its
turns and attached extracted text, and cancels its pending action proposals.
Earlier chats that existed only in an old browser tab cannot be recovered.

History belongs to its operator. Question/answer payloads and extracted file text
are encrypted at rest with the installation key (chat titles and file names are
metadata). Changing section grants or the AI category blacklist hides history
created under the old policy. Each turn also rechecks its referenced tickets;
follow-ups inherit those references, including action-only proposals. Inaccessible
or deleted tickets cause those answers to be redacted. History reads never run a
model; reopening a confirmation refreshes its actual applied/expired state.

Use the paperclip for UTF-8 **TXT, MD, CSV, JSON, LOG** or **DOCX** documents.
Up to three files per message, 2 MiB and 24,000 extracted characters per file.
Original bytes are parsed and discarded; click the file chip to preview the
extracted text. DOCX external entities and oversized compressed content are
rejected. PDF, images and spreadsheet binaries are not supported in this version.
The selected files share a 24,000-character model context budget; any truncation
is explicitly marked for the model. Recent chat attachments carry into follow-ups.
Files are private to their operator and bound to one conversation after sending.

Storage limits: 100 conversations, 100 turns per conversation and 300 files per
operator. Unattached files older than 24 hours are pruned on subsequent uploads.
Delete unneeded conversations to free space. No full prompts or provider error
bodies are logged. History intentionally stores answers, unlike the audit tables.

Answers support tables, headings, lists and code using a bounded DOM renderer.
Model-supplied HTML, links and images remain inert text; clickable source links
come only from server-owned readers. Existing dashboard colors remain unchanged.

## Confirmation and cost

Approval IDs are random, expire after 10 minutes and are bound to the operator,
session generation and observed ticket state. The server rechecks permissions,
category policy, ticket state and assignee eligibility, then atomically records
the mutation and consumed approval. Replays, another operator's approval, stale
tickets and concurrent confirmations cannot apply the same approval twice.

There are at most two model calls per question, three read tools, 20 questions
per operator per hour and 200 per installation per hour. These caps are stored
and checked through PostgreSQL, so multiple dashboard processes share them.
There is no automatic model retry. Model unavailability leaves the dashboard
usable and does not execute a proposal.

Each analysis page is a sample (newest matching tickets first), not an assertion
that the entire workspace was read. Excerpts are bounded to 320 characters of
the opening, 220 of the latest customer message and 280 of the latest delivered
operator reply. Use ticket detail for deeper investigation. Total serialized
answer context is capped at 96,000 characters. The previous period's closed
count is queried directly rather than estimated by subtracting unrelated counts.
Request IDs deduplicate a replay after a lost response without a second model call.

Interactive calls have a 60-second deadline each (at most two calls); background
suggestions retain their 8-second provider timeout. Opus 5.5 and the other listed
always/adaptive-thinking 5.5 models use low effort with explicit total output caps
of 4,096 tokens for planning and 6,144 for the answer, including thinking. Other
models retain the 600/1,400 caps. These are ceilings, not predicted usage.

The assistant opens at the top of the sidebar, with a full-height conversation
and a bottom composer. Enter sends; Shift+Enter inserts a new line. Failed requests
keep the draft. Provider errors have separate translated categories without raw
provider bodies or credentials in the UI/logs; they do not expire the login session.

## Anthropic setup and troubleshooting

HTTP 401 means the selected endpoint rejected authentication, which can mean a
provider mismatch rather than a broken key. For direct Anthropic, enter an Anthropic API
key in AI settings. For an OpenRouter key, select OpenRouter instead. A Claude web/Code
subscription is not the API key used by this connector. The exact Opus 5.5 API ID
is `claude-opus-5-5`, with hyphens, not `claude-opus-5.5`. New dropdown entries use
the documented IDs. Existing saved selections and credentials are not overwritten.

Opus 5.5 always uses adaptive thinking and rejects custom sampling parameters.
The adapter omits temperature and reads only text blocks; thinking is never
displayed as the answer. A truncated response cannot become an action proposal.
Provider 403/404, billing, rate limits, timeouts, empty and malformed responses
produce distinct messages. See the official [error reference](https://platform.claude.com/docs/en/api/errors),
[model catalog](https://platform.claude.com/docs/en/models/overview) and
[Opus 5.5 migration guide](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide),
checked 2026-10-01.

## OpenRouter setup

In **AI settings → Provider**, choose **OpenRouter**, enter your OpenRouter API
key and select a model or paste its full catalog ID, e.g. `anthropic/claude-opus-5.5`.
Save the engine form. The environment fallback is `OPENROUTER_API_KEY`.
This connector always uses `https://openrouter.ai/api/v1/chat/completions` with
Bearer authentication, never the host left by another provider. Credentials are
encrypted and stored separately per provider; switching providers does not copy keys.

OpenRouter IDs differ from direct Anthropic IDs: `anthropic/claude-opus-5.5`
versus `claude-opus-5-5`. The listed mandatory-thinking Claude models get the same
bounded assistant budgets and low reasoning effort. Only final text is displayed;
HTTP 200 error envelopes, truncated output and refusals cannot become proposals.
Unknown model IDs pass through unchanged; availability and custom-model parameter
support are determined by OpenRouter. No automatic retries or live API tests run.

References: [authentication](https://openrouter.ai/docs/api/reference/authentication),
[errors](https://openrouter.ai/docs/api/reference/errors-and-debugging),
[reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens),
and the [public model catalog](https://openrouter.ai/api/v1/models), checked 2026-10-01.

`ai_calls` records model/token/latency metadata. `assistant_runs` records status
and limits, with 30-day retention. `assistant_actions` records the exact proposed
and confirmed changes; expired unused approvals are pruned. Full prompts and
answers are not stored in these audit tables.

Verification uses fake models and disposable databases, never live providers,
customer mailboxes or the canonical preview database. See `tests/test_assistant.py`,
`tests/test_assistant_extended.py` and `tests/frontend/assistant-markdown.test.cjs`.
