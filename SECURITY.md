# Security

## Reporting a vulnerability

Please report privately, not as a public issue:

- GitHub → [Security → Report a vulnerability](https://github.com/spooldesk/spool/security/advisories/new)
- or email **hello@spooldesk.net**

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
- Bypassing section permissions, owner-only operations or private assistant history
- The AI path: prompt injection that makes a deflection answer leak an internal
  note, or the scrubber failing to redact a credential before it reaches the
  model

## What is not

- Findings that need an operator account you already control to be malicious
  with data you already have access to
- Capacity claims beyond the tested deployment size; resource-exhaustion defects
  in ordinary authenticated or public workflows are still useful reports
- Missing hardening headers on a deployment you configured yourself
- Disclosed credentials for the local synthetic preview. That configuration
  must never be used for a public production deployment or real customer data.

## Running it safely

- **Serve over HTTPS and set `DASH_COOKIE_SECURE=true`.** The session cookie is
  the whole of dashboard authentication.
- **Do not bind the dashboard to `0.0.0.0` without a reverse proxy.** It has no
  IP allowlist of its own.
- Keep `.env` out of version control — it is in `.gitignore`. Review release archives and
  Git history for accidental disclosure.
- New installations use PostgreSQL. Ticket conversations and stored attachments
  are sensitive data; protect the database, file storage and backups. Keep the
  encryption key separately: integration credentials and AI chat payloads need it.
- Give each operator their own account rather than sharing one. Roles are
  enforced per route, and the audit trail is only as good as the accounts.

## What the AI sees

When AI is used, ticket text is sent to the configured provider: Gemini,
Anthropic, OpenAI, OpenRouter, or the configured Ollama endpoint. Before it leaves:

- Credential-shaped strings are redacted by `src/ai/scrubber.py`
- **Internal notes are never included.** An answer generated for a customer
  must not be built from what the team said privately — there is a test for
  this, and it is the one I would most like to hear about if it is wrong.

Category exclusions and AI modes are configured in the dashboard. The operator
assistant separately checks section permissions and requires confirmation before
applying a proposed change. Its private history and explicit document uploads
are described in [AI_ASSISTANT.md](docs/AI_ASSISTANT.md). Teams route work and do
not provide tenant isolation. This edition is one installation per organization.
Ollama can run without an API key; other providers require credentials.
