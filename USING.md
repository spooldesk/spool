# Using Spooldesk

Operators work in the web dashboard. Email, Slack, website chat and optional
Telegram are customer entry points. The old Telegram operator reply controls
are disabled; use the dashboard on desktop or a suitable mobile browser.

## Start as the owner

1. Sign in with the owner credentials generated for this installation.
2. Configure only the needed channels in **Integrations**. Save, check access,
   then confirm a real incoming message and the recipient's receipt of a reply.
   Authentication checks alone do not prove end-to-end delivery.
3. Invite colleagues in **Operators**. Give each person their own account.
4. Use **Roles** for section visibility and view/edit permissions. Owner-only
   operations, including connection credentials, remain owner-only.
5. Configure teams, categories, working hours and response targets as needed.

See [connection diagnostics](docs/CONNECTION_CENTER.md). Dashboard language,
theme and row density are individual operator preferences; customer language
settings govern customer-facing messages separately.

## Work through a ticket

The active queue and closed archive are separate. Open a ticket from its list
or board card; review the customer's channel, history, assigned operator and
verification state before replying.

- Assign responsibility and use New / In progress / Waiting as work stages.
- A delivered public reply puts the conversation into waiting for the customer;
  internal notes do not count as customer answers. A new customer reply resumes
  the response clock. Working hours and the configured response policy apply.
- Switch explicitly between a public reply and an internal note. Notes stay
  operator-only and are not sent as customer messages or ordinary AI context.
- Type a reply, attach files or paste an image; preview attachments before sending.
  Text areas grow automatically. `Ctrl+Enter` sends from the reply composer.
- The three-dot message menu contains the available reply, copy, edit and removal
  actions. External-channel capabilities govern which mutations are possible.
- Close after resolution using the configured reasons. CSAT is available on
  supported channel flows; it is not a guarantee that every recipient responds.

## Colleague typing indicator

When a different operator types, the ticket header and composer show their name
and whether it is a customer reply or internal note. Own tabs are not shown as
colleagues; multiple tabs of one colleague appear once. The indicator expires
after inactivity and clears on normal departure. It transmits activity flags,
not draft text. A connection failure must not be treated as proof nobody else
is answering. The indicator is advisory; it does not lock the Send button.

## Delivery problems

Watch message delivery state. Queued, failed and uncertain are different states.
A provider may have accepted a message even if its acknowledgement was lost.
Inspect the customer channel before explicitly retrying an uncertain send.
The owner can review connection and delivery diagnostics in Integrations.
Email edits/deletions cannot recall a delivered email from an external inbox.

## Customers and verification

Approve incoming customers and assign a team when that workflow is enabled.
Disabling multi-team routing leaves the single-team workflow. Linking email,
Slack or Telegram identities groups a customer's contact history; it does not
prove identity automatically. Verify ownership before linking accounts or
disclosing private information. Widget-signed identity and support approval
are different checks. See [deployment](DEPLOY.md) for signed widget identities.

## Knowledge and templates

Keep internal drafts separate from published help articles. Only publish content
appropriate for customers. Templates speed up repeated replies; review them
before sending. Configure company information/documents in AI settings to give
the model terminology, policies, escalation rules and examples; do not upload
secrets. Providing context is not fine-tuning the underlying model.

## AI assistant

Enable the assistant in AI settings and select the correct provider, model and
key. OpenRouter uses its own provider selection. The assistant can search ticket
content, summarize issues, create drafts and propose supported changes.
Inspect sources and coverage; it does not necessarily read every message.
Confirm status/assignee changes only when the proposed action is correct.

Chats and supported document excerpts are private to the operator and encrypted
at rest. History survives reload; current access is checked again when it is
read. Upload text formats or DOCX to the chat. PDF/image OCR and arbitrary agent
code execution are outside this release. See [AI_ASSISTANT.md](docs/AI_ASSISTANT.md).

## Reports and working preferences

Use overview and reports for queue, response/resolution metrics, CSAT and period
comparisons. Check date ranges, filters and metric definitions before sending a
report to a lead. AI accept rate is not a support-team performance KPI.
Optional automatic-next-ticket and notification preferences are per operator.
Shortcuts available on a page are shown by that page's shortcut help; browser
permissions and OS-level shortcuts can affect notifications and key handling.
