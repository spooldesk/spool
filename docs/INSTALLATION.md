# Install Spooldesk

Use this guide for a new installation on Windows, macOS or Linux. One instance
serves one organization. The installer starts Spooldesk and PostgreSQL locally.

## Requirements

- Windows/macOS: Docker Desktop running with Linux containers.
- Linux: Docker Engine running, Compose v2 or newer, and permission to use Docker.
- An x86-64 or ARM64 computer and internet for the initial PostgreSQL download.
- A permanent, writable folder belonging to your user account.

Python and Git are not required for the installer. Docker is installed separately;
the installer does not change system permissions or Docker settings.

## Download and start

1. Download **spooldesk-v0.1.0-rc.9-installer.zip** from the
   [release page](https://github.com/spooldesk/spool/releases/tag/v0.1.0-rc.9).
   Choose the installer ZIP, not GitHub's automatic Source code download.
2. Extract the entire ZIP to a permanent personal folder. Do not run it inside
   the ZIP, move it after setup, or use Program Files or a shared writable folder.
3. Start Docker, then open the launcher for your OS:

| OS | Open |
| --- | --- |
| Windows | `INSTALL-WINDOWS.cmd` (double-click) |
| macOS | `INSTALL-MACOS.command` (open in Terminal) |
| Linux | `INSTALL-LINUX.sh` (Run in Terminal) |

macOS/Linux may require permission to execute the downloaded file. This is a
terminal wizard, not a signed/notarized native application. On Linux, use your
file manager's Run in Terminal option or allow execution in file properties.

## Complete setup

1. Select `1` for English or `2` for Russian.
2. Choose the dashboard port, normally `8000`. Use an available port; `8011` is
   reserved. Ports below 1024 are not supported by this installer.
3. Enter a password of 12–128 characters using the displayed characters, or press
   Enter to generate one. Input is hidden. A custom password must be repeated.
   Only an automatically generated password is displayed; save it securely.
4. Enter **1** or press **Enter** to install. Enter **0** to cancel. Invalid
   answers are explained and requested again. Cancellation does not create a
   configuration or database and is not a Docker error.
5. Wait for PostgreSQL and Spooldesk to start. The dashboard opens automatically.

Default address: **http://localhost:8000**. Initial owner: **admin@local**.
Use the password you entered or saved. Your password and encryption keys are
independent of any other installation.

## First use and later starts

Create individual operator accounts. Connect email, Slack, Telegram or a website
widget in **Integrations**; configure optional AI in **AI settings**. Check an
incoming ticket, a delivered reply and an attachment with your test accounts.
Avoid running two installations against the same mailbox or bot account.

For later starts, open the same launcher from the same folder. It preserves the
database and keys. The `spooldesk/` folder contains configuration; Docker volumes
contain PostgreSQL and attachments. Keep both. Do not delete volumes to fix an
installation error. Re-run after a restart and verify your records remain.

This wizard creates a new installation or starts its own existing installation.
It does not upgrade an older instance or overwrite another installation.
For upgrades and public HTTPS access, see [Deployment](https://github.com/spooldesk/spool/blob/main/docs/DEPLOYMENT.md) and
[Backup and restore](https://github.com/spooldesk/spool/blob/main/docs/BACKUP_RESTORE.md).

## Files in the installer ZIP

| File or directory | Purpose |
| --- | --- |
| `README.txt` | Start here: launchers and links to guides |
| `INSTALLATION.md` / `INSTALLATION_RU.md` | English / Russian installation guide |
| `TROUBLESHOOTING.md` | Installation problems and recovery steps |
| `INSTALL-*` | Launchers for the three operating systems |
| `installer/` | Setup scripts; leave these files unchanged |
| `package/` | Application images, source archives and release metadata |
| `SHA256SUMS` | Checksums verified automatically by the launcher |
| `spooldesk/` | Your installation; created after confirmation |

See [Troubleshooting](TROUBLESHOOTING.md) if a step stops. Report the step and error
text, excluding passwords, API keys and `.env` contents.
