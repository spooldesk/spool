# Frequently asked questions

## What am I installing?

A self-hosted helpdesk for one organization. Customers contact it through
email, Slack, the website widget or optional Telegram. Operators reply from
the web dashboard. Telegram group reply controls are retired.

## Do I need a VPS?

No purchase is required for a local installation test. A suitable computer
with Docker running Linux containers can host it. Public use needs a reliable,
continuously available machine, persistent storage and HTTPS. Sleep, power loss
and a disconnected network make a laptop installation unavailable.

## Is PostgreSQL included?

Yes. Standard Compose starts PostgreSQL 16 and the dashboard, with separate
named volumes for the database, attachments and logs. SQLite is for isolated
tests and legacy recovery; it is not the recommended production configuration.
No operator-capacity or RAM claim has been established by a load test.

## What do I pay for?

Own production use within the current [BSL grant](LICENSE) has no mandatory
software fee. You pay your infrastructure, email and AI providers. Installation
and maintenance are optional, separately scoped services. The landing's service
figures are indicative; commercial arrangements are still being prepared.
There is no checkout or hosted SaaS subscription in this release.

## Is it open source? Can I resell it?

The source is available under BSL 1.1, which has use restrictions and is not an
OSI open-source licence. The licence defines conversion to Apache 2.0 on the
specified change date or earlier applicable fourth anniversary. Hosted/managed
offerings to third parties outside the grant need separate rights. Do not infer
unlimited MSP/reseller rights simply because the machine belongs to a client.

## Are channels and AI required at first start?

No. Start the clean dashboard, then configure needed channels in owner-only
**Integrations**. The assistant is optional and has its own settings. OpenRouter
keys must use the OpenRouter provider, not the direct Anthropic provider.

## Will my customer data stay on my machine?

The database and local attachments are stored on your infrastructure. Connected
email/messaging services and any external AI provider also process data needed
for their enabled functions. AI analysis uses bounded ticket excerpts and
explicit uploads, with section permissions and internal-note exclusions.
Self-hosting is not an automatic GDPR or other privacy-law compliance claim.

## Can the assistant change tickets?

It can propose supported status/assignee changes for a permitted ticket.
You must confirm the concrete action, and permissions/current state are checked
again. It can search, summarize and draft, but has no arbitrary shell access.
Private chat history survives reload; TXT/MD/CSV/JSON/LOG/DOCX uploads are supported,
not image/PDF OCR. See [the assistant guide](docs/AI_ASSISTANT.md).

## How do colleagues avoid double replies?

An indicator beside the ticket composer names colleagues typing a reply or an
internal note. Tabs of one operator are deduplicated; no draft text is shared.
This is advisory, not an exclusive lock. Two independently sent replies can still
both be delivered, so operators should coordinate responsibility.

## Can I edit or delete a sent message?

Use its three-dot menu. What reaches the external channel depends on its
capabilities, permissions and delivery state. Email cannot generally be recalled
or edited in the recipient's inbox. Uncertain delivery requires investigation;
do not assume a retry cannot duplicate a message.

## How are customers and teams handled?

Customer approval/team assignment is distinct from an operator's dashboard
account and from cryptographic proof of a widget identity. Channel accounts can
be linked to a customer profile after verifying they belong together. Teams can
be disabled for a single-team workflow. They do not provide tenant isolation.

## Backup, restore and upgrade?

Use the PostgreSQL backup tool for DB plus attachments and preserve the original
encryption key separately. Restore on new storage and review quarantined jobs.
Back up before upgrading; an old image is not guaranteed to understand a newer
schema. See [recovery](docs/BACKUP_RESTORE.md) and [release procedure](docs/RELEASING.md).

## Does installing successfully mean the whole release is ready?

It establishes portability when tested from the supplied archive on a clean
computer. Combine that result with the functional tests already completed,
reviewed source and matching release artifacts. A customer's actual channel
credentials, HTTPS, backups and monitoring still need deployment-specific checks.
Paid services also need seller and contract arrangements; see
[legal preparation](docs/LEGAL_READINESS.md).
