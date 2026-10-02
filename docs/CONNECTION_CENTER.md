# Connection setup and delivery diagnostics

The owner-only **Integrations** page (`/manage/integrations`) separates saved
configuration, provider authentication, incoming messages and delivered replies.
It helps an owner finish setup and investigate a broken conversation without
reading server logs or assuming that a saved token means a working channel.
English and Russian use the current operator's interface language.

## First conversation

1. Save the channel's settings. Blank secret fields preserve existing secrets.
2. For Slack, save the public HTTPS origin, switch Socket Mode off in Slack,
   verify the generated Events URL and subscribe to `message.im`. Reinstall the
   Slack app after changing permissions. For a website, save the widget's own
   public HTTPS origin and allowed website origins, then insert the generated
   script into that website. The Slack-only tunnel policy does not expose the
   widget. Saving a public address does not create hosting, a tunnel or DNS.
3. Click **Check connection** for Slack, Telegram or email. The button checks
   saved settings, so save edits first. No check runs on page load.
4. Send a new customer message through the channel and confirm its ticket
   appears. Reply from the dashboard and verify receipt in the customer's
   channel. For the website, test on the actual allowed website as well as the
   local widget preview. Test an attachment separately.

Opening `/slack/events` in a browser sends GET; the callback expects a signed
POST, so a local 405 is expected. Slack's **Verified** status confirms its URL
challenge. Only a new incoming message confirms the complete intake path.

## What the page proves

| Indicator | Meaning and limits |
| --- | --- |
| Settings saved | The channel is enabled and required fields are present. |
| Public address specified | A syntactically valid public HTTPS origin was saved. DNS, reachability and certificates have not been checked. |
| Access checked | The provider accepted authentication at the displayed time. Slack also reports missing scopes; this does not verify event subscriptions or the signing secret. |
| Incoming message received | An incoming customer message was stored after the current settings were saved. A challenge request alone does not count. |
| Reply delivered | An outbound job completed after the settings were saved. This means acceptance by the channel, or publication to the widget; it does not prove customer receipt or reading. |

Historical last-activity timestamps remain visible after configuration changes,
but do not complete the new configuration's conversation steps. All displayed
timestamps use UTC. With environment-only settings, traffic must follow the
current explicit check; saving the configuration gives it a stable start time.
The progress count is evidence of setup, not an uptime guarantee: inspect the
separate delivery-problem count even when all setup steps are complete.

Results expire after 24 hours and become stale immediately when the connection
configuration changes. The UI discards a check that finishes against an obsolete
configuration. Checks have a 30-second cooldown and a database lease prevents
concurrent checks for the same channel across application processes.

## Provider checks and privacy

- Slack: `auth.test` and granted scopes. No messages are sent or read.
- Telegram: `getMe` and `getWebhookInfo`. An existing webhook is reported because
  the current adapter uses polling. The check does not consume updates, delete
  a webhook or send a message.
- Email: TLS IMAP login and read-only INBOX selection; independently, TLS SMTP
  login. No message fetch, mark-as-read, delete or send. TLS certificate checks
  remain enabled. IMAP and SMTP can succeed or fail separately.
- Widget: no credential probe; use the actual website and conversation steps.

Only allowlisted result codes, known missing Slack scopes, a timestamp and a
configuration digest are stored in `integration_check_<channel>` settings.
Provider responses, exception text, tokens and message bodies are not saved in
diagnostic results. The page never infers public callback URLs from the request
Host header. The owner must explicitly supply them; Slack and widget origins
are independent. An optional `SLACK_PUBLIC_URL` environment fallback is supported.

## Messages requiring attention

The page shows totals and up to 25 oldest entries in each direction:

- Outbound `failed` and `uncertain` jobs; `sending` older than the delivery
  worker's five-minute lease; `pending`/`retry` overdue by 15 minutes.
- Inbound `pending` events with a recorded processing error, or queued longer
  than 15 minutes. An inbound error can still be waiting for an automatic retry.

An ordinary outbound retry scheduled for the future is not flagged as stalled.
Cancelled jobs are intentional cancellations. Delivered jobs are historical
activity, not problems. Unknown delivery means the provider may already have
accepted the message: check the customer's channel before any explicit retry.

Problem rows contain only IDs, channel, status, attempts and timestamps. Outbound
rows link to the existing ticket for investigation and its controlled retry
action. The diagnostics page never replays or resends anything. An inbound event
may not yet have a ticket; its event ID identifies it for server investigation.

## Operations and validation

No new infrastructure, scheduled probes or database migration is required.
Restart the dashboard after installing these Python/template changes together.
Existing connections and the canonical local database are retained.

`tests/test_connection_center.py` exercises the feature with mocked providers
and a disposable PostgreSQL database: owner/CSRF access, URL validation, stale
results, concurrency, redaction, queue classification and checks without sending
messages. Real-channel acceptance still requires the manual conversation above.

Verification on 2026-09-30: 287 targeted PostgreSQL tests passed across connection
diagnostics, workspace access, runtime channels, Slack, widget, health, translations
and static assets. A separate browser instance with synthetic data confirmed the
check button's pending/completed states, saved results, a simulated SMTP failure,
theme switching and no horizontal overflow at 390 px. Screenshot capture timed
out, so a complete visual review remains outstanding. No live-channel message was
sent as part of this verification.
