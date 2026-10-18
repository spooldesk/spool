# Customer AI agent

Spooldesk has two separate AI surfaces. The [workspace assistant](AI_ASSISTANT.md)
helps employees search, analyse and draft. The customer agent replies to inbound
support messages through Telegram, Slack, email and the website widget. Both use
the provider and model selected in **AI settings**; OpenRouter is supported.

## Configure

1. Configure the provider/key and enable the main AI switch.
2. Add a company document (DOCX, TXT or Markdown), published articles and relevant
   team templates. Explain your products, supported troubleshooting, escalation
   conditions, policies, links and facts the agent may quote. Never include secrets.
3. In **AI settings → Customer AI agent**, choose **One reply** or **Continue the
   conversation**. Single reply remains the default; old installations are not
   silently switched to a more autonomous mode.
4. Set tone and up to 4,000 characters of instructions. Clarification is available
   in conversation mode. General troubleshooting outside the supplied knowledge
   is a separate opt-in; company facts must still come from references.
5. Set reply-attempt limit (1–20, default 6), recent context messages (4–40, default 16),
   output budget (256–8192 tokens, default 1500) and timeout (10–120 seconds,
   default 45). Larger budgets can increase provider costs. Reasoning models may
   need more output tokens. There is no automatic model-call retry or fallback
   to another paid provider. Logged failed or withheld model attempts also count
   toward the limit; receiving an error does not reset the cost guard.
6. Enable **Automatic replies**. To evaluate without sending, enable the existing
   shadow/trial mode. It generates and records answers but never delivers them.

Saving the policy does not enable AI, change your key or alter existing tickets.
Only newly received messages while automatic replies are enabled are queued.
No historical conversations are replayed when installing this version.

## Conversation and handoff

Replies use bounded customer-visible history. Internal notes, deleted messages,
undelivered messages and original attachment contents are excluded. Customer
uploads alone are not read by this agent; attaching files to the employee AI
chat is a separate capability. References are supplied as model context, not
used for fine-tuning. They are processed by your selected provider.

The agent stops when a human is assigned or sends a public reply, a customer asks
for an operator, a prior answer is rejected, the turn limit is reached, or an
earlier delivery needs attention. New customer messages or edits during generation
withhold stale answers; the latest queued message can then be processed. A handoff
keeps the ticket open for the normal operator queue. It does not invent a solution,
reset SLA, assign an arbitrary employee or close a ticket. Customer confirmation
through a supported channel's **Solved** choice still uses the existing close flow.

The next ticket starts a new AI conversation. Re-enabling AI does not override a
customer's handoff on the same ticket. Already-started network sends cannot be
recalled atomically if an operator takes over at that exact moment.

## Diagnose missing answers

Open **AI Analytics → Automatic reply outcomes**. The report separates delivered,
withheld, generation/delivery failures, queued and trial-only decisions. Its latest
50 rows link to tickets and show stable reasons, model, latency and token counts.

Examples: no knowledge, missing provider/key, insufficient evidence, operator
takeover, single-reply/turn limit, newer message, changed reference, timeout,
provider authentication/billing/rate limit, exhausted output budget, channel error
or uncertain delivery. Successful generation is not counted as delivery.
Earlier skips that were never logged cannot be reconstructed retroactively.

The model-failure list also includes other AI features. Provider error bodies and
credentials are not displayed. An uncertain delivery needs operator reconciliation;
do not blindly resend it. A process interrupted during generation is recorded as
interrupted after its lease interval, without silently paying for another call.

## Operations

The new `ai_auto_turns` table is created additively by the existing database
initialization. Inbound messages and queue entries commit together. Unique inbound
message IDs and transactional claims prevent duplicate model work; the outbox
stores delivery intent before contacting a channel and rechecks policy on retries.
The dashboard process owns the polling loop. The optional legacy receiver can also
process turns; claims serialize per ticket across processes.

After an upgrade, restart the application so code, templates and translations use
the same version. Back up your self-hosted installation before upgrading. Existing
data, credentials and enable/disable switches are preserved.
