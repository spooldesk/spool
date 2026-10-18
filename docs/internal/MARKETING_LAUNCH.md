# Spooldesk first customer campaign

Prepared 15 October 2026. Start with small B2B software and service teams that want support on their own infrastructure. The first goal is three qualified pilots, not traffic volume. The numbers below are operating targets, not forecasts or claims about existing customers.

## Positioning

**English:** Spooldesk is a self-hosted helpdesk that brings email, Slack, Telegram and website enquiries into one workspace, with an optional AI assistant under your team's control.

**Russian:** Spooldesk — helpdesk на вашей инфраструктуре: почта, Slack, Telegram и обращения с сайта в одном кабинете, с необязательным ИИ-ассистентом под контролем команды.

Primary buyer hypothesis: a founder or support lead with 2–10 support operators and a technical colleague who can maintain Docker, backups and HTTPS. Qualify the need for self-hosting before discussing AI. Avoid positioning the product as Telegram-only or as a managed SaaS service.

The current installer is rc.9. The team assistant is available; continued automatic customer dialogue and expanded diagnostics are previews of a later release. The landing demo uses fictional data and scripted answers. Say **source-available under BSL 1.1**, not OSI open source. Internal production use is allowed by the Additional Use Grant; offering a competing hosted/managed service requires separate licensing. Link [LICENSE](../../LICENSE) rather than paraphrasing permissions in a contract.

## First pilot offer

Proposed scope: one customer-managed installation, two operators, up to two channels, 14 days, one onboarding call and one review call. No historical migration, custom integration, managed hosting or round-the-clock SLA is included. Agree a separate scope for those services. Confirm pricing, contracting entity and payment readiness before making a paid offer; the software download itself does not commit the founder to free support.

Success criteria agreed before starting:

1. The customer installs the pinned release and can restore a backup.
2. A real enquiry arrives and receives an answer in each selected channel.
3. Two operators work without duplicate replies and understand assignment/waiting states.
4. The customer identifies a measurable benefit, such as fewer missed enquiries or less time finding context. Record the before/after method; do not invent a percentage.
5. The buyer explicitly chooses to continue, stop or request a scoped service. Ask separately for permission to publish a quote or case study.

## Two week execution

| When | Action | Evidence to keep privately |
| --- | --- | --- |
| Days 1–2 | Verify website events; record a 60–90 second workflow demo using fictional data; complete GitHub description/topics | Working installer/demo links and baseline date |
| Days 2–4 | Select 20 relevant companies from existing contacts, public company pages and communities permitting product feedback | Company URL, public business contact route, specific fit signal; no scraped personal emails |
| Days 3–8 | After approving recipients and text, send up to five individually relevant invitations per day; target ten discovery conversations | Sent date, reply, main pain, next action, opt-out |
| Days 5–12 | Run up to three scoped pilots | Installation outcome, channel check, support effort, objections |
| Days 12–14 | Review activation and willingness to pay; improve the dominant blocker | Continue/change/stop decision per pilot |

Five discovery questions: Which channels carry support? How many operators and enquiries? Why must data stay on your infrastructure? Who maintains the installation? What problem would make a switch worth the effort and budget?

## Channels and ready drafts

These are drafts for founder approval, not published messages. Do not use unrelated GitHub issues as advertising, mass-message community members, ask for upvotes or claim customers/testimonials that do not exist.

### LinkedIn founder post

> I’m building Spooldesk, a self-hosted helpdesk for teams whose support is spread across email, Slack, Telegram and their website.
>
> You run the workspace on your own infrastructure. The optional AI assistant can search tickets, read public conversations and prepare drafts; status and assignee changes require confirmation.
>
> The release candidate can be installed with Docker, and there is a no-signup interactive demo. I’m looking for three small teams to try a clearly scoped pilot and tell me where the workflow or installation gets in the way.
>
> Which is harder for your team today: keeping track of requests, finding context, or maintaining the tools?
>
> https://spooldesk.net/?utm_source=linkedin&utm_medium=social&utm_campaign=first-pilots-2026-10#product

### Personal invitation

> Hi [name]. I noticed [specific public fact about the team's support workflow]. I’m building Spooldesk, a self-hosted workspace for support across email, Slack, Telegram and a website widget. Would keeping those enquiries together on your own infrastructure solve a problem for your team? If it would, I can share the demo and discuss a 14-day pilot. If not, no follow-up needed.

Replace both placeholders with verified details. Send only after approving the specific audience and channel. One follow-up after 4–5 business days is enough; stop on refusal or opt-out. Do not send an automatic sequence to downloaded contact lists.

### Show HN

The founder must write the title, post and comments personally and submit them manually. The [HN guidelines](https://news.ycombinator.com/newsguidelines.html), checked 15 October 2026, prohibit generated text and automated posting. Do not use an AI-generated draft or have an assistant rewrite the submission.

Factual checklist for the author, not copy to submit:

- Prefix the title with `Show HN:` and describe the actual project plainly.
- Link the [runnable GitHub release](https://github.com/spooldesk/spool/releases/tag/v0.1.0-rc.9). A landing page alone is not a Show HN project.
- Explain the personal motivation and the feedback being sought in the founder's own words.
- Disclose that this is a release candidate, Docker is required, and the license is BSL 1.1. Link the license rather than promising broader rights.
- If mentioning the browser demo, describe its fictional data and scripted responses accurately.
- Be available to answer technical questions; do not solicit votes.

Read [Show HN rules](https://news.ycombinator.com/showhn.html) immediately before posting. For Reddit/self-hosted communities, check their current self-promotion rules and licensing criteria before submitting; do not assume every directory accepts BSL.

## Measurement and sales automation

Use [website analytics](../WEBSITE_ANALYTICS.md) for visits, demo interaction, installer-link clicks and contact-link clicks by coarse source. A contact click is not an email, an installer click is not an installation, and counts are not unique people.

Keep an access-restricted lead board outside the public repository with stages: New → Qualified → Demo → Pilot → Offer → Won / Lost. Store only necessary business details, source, agreed next step/date and consent/opt-out. Actual emails, installations and purchases must be confirmed separately. Never commit lead identities or conversation content here.

Start automation with inbound classification, a draft reply and a next-action reminder. Let a person approve outgoing replies, prices and commitments. Do not connect the landing demo to a real model or auto-send prospecting messages. Automation cannot establish a viable offer before discovery conversations do.

Decision rules: relevant visits without demo interest suggest positioning issues; demo interest without installs suggests trust/setup friction; installs without channel activation suggest onboarding problems; active pilots without willingness to pay suggest a pricing/value mismatch. Review only after enough relevant conversations to interpret the counts. Do not buy ads merely to increase page views.
