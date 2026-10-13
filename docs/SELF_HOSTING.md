# Self-hosting Spooldesk

Each installation serves one organization. You control its infrastructure,
database, attachments, credentials and external service accounts. The installer
starts the dashboard and PostgreSQL; enabled channel workers run with the dashboard.
The marketing website is deployed separately.

Start with [Installation](INSTALLATION.md) or [Установка](INSTALLATION_RU.md).
For daily work see [User guide](USER_GUIDE.md), [Integrations](INTEGRATIONS.md)
and [AI assistant](AI_ASSISTANT.md).

## Local use and public operation

The installer binds the dashboard to localhost. A computer is suitable for a
local evaluation; sleep or shutdown makes the service unavailable. Public use
requires appropriate uptime, HTTPS and a maintained host. External Slack HTTP
events and a website widget need a reachable HTTPS endpoint.

Follow [Deployment](DEPLOYMENT.md) for public access and configuration. Keep
database and attachment storage persistent. Establish backups, retain the
encryption key separately and test [restore](BACKUP_RESTORE.md) before relying
on the installation. Do not run two instances against the same channel accounts.

## Updates and support

Use a versioned release with checksums. Back up before upgrading; the installation
wizard does not migrate a different existing installation. Keep a rollback image
and a pre-upgrade backup. Assign responsibility for monitoring and updates.

Self-hosting and third-party AI/messaging services are separate: each enabled
provider needs its own account and may have usage charges. Review data handling
for the channels and AI features you enable. Usage rights are defined by
[LICENSE](../LICENSE). Installation assistance is available at hello@spooldesk.net.

Release candidates are prereleases. Review [Release notes](RELEASE_NOTES.md) and
perform an installation, sign-in, ticket/reply, attachment and restart check on
your own host before customer use.
