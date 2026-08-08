# Security

## Reporting a vulnerability

Please report privately, not as a public issue:

- GitHub → [Security → Report a vulnerability](https://github.com/spooldesk/spool/security/advisories/new)
- or email **boykokuza12@gmail.com**

I maintain this alone alongside a job, so: expect a first reply within a few
days, not hours. Tell me what you did, what happened, and how bad you think it
is — a proof of concept helps more than a severity rating.

Please do not test against someone else's running instance.

## What is in scope

The bot, the dashboard, and anything in this repository. Most usefully:

- Authentication and session handling — the signed cookie, CSRF, login rate
  limiting, password storage (argon2)
- Role checks: an `agent` reaching an `admin` or `owner` route
- SQL injection, template injection, XSS in operator-supplied content
- Anything that lets one team read another team's tickets
- The AI path: prompt injection that makes a deflection answer leak an internal
  note, or the scrubber failing to redact a credential before it reaches the
  model

## What is not

- Findings that need an operator account you already control to be malicious
  with data you already have access to
- Denial of service through sheer volume — it is a single SQLite process and
  makes no claim otherwise
- Missing hardening headers on a deployment you configured yourself
- The hosted demo. It is seeded with synthetic data, resets itself, and its
  credentials are printed in the README on purpose.

## Running it safely

- **Serve over HTTPS and set `DASH_COOKIE_SECURE=true`.** The session cookie is
  the whole of dashboard authentication.
- **Do not bind the dashboard to `0.0.0.0` without a reverse proxy.** It has no
  IP allowlist of its own.
- Keep `.env` out of version control — it is in `.gitignore` and has never been
  committed.
- `support.db` holds every conversation in plain text. Back it up, and treat
  the backup like the conversations.
- Give each operator their own account rather than sharing one. Roles are
  enforced per route, and the audit trail is only as good as the accounts.

## What the AI sees

If AI features are enabled, ticket text is sent to Google Gemini. Before it
leaves:

- Credential-shaped strings are redacted by `src/ai/scrubber.py`
- **Internal notes are never included.** An answer generated for a customer
  must not be built from what the team said privately — there is a test for
  this, and it is the one I would most like to hear about if it is wrong.

Categories can be excluded from AI entirely in the dashboard, and the whole
feature is off unless you configure a key.
