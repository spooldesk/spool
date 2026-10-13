#!/usr/bin/env bash
# Bash 3.2 compatible (including the system shell on macOS).
set -euo pipefail
export DOCKER_CLI_HINTS=false
bundle=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$bundle"
lock_container=
finish() {
  result=$?
  if [ -n "$lock_container" ]; then docker rm "$lock_container" >/dev/null || true; fi
  if [ "$result" -ne 0 ]; then
    printf '\nInstallation stopped. Existing data was not removed. Keep this folder and fix the error above before retrying.\n'
  fi
  if [ "${SPOOLDESK_INSTALLER_NO_PAUSE:-}" != 1 ] && [ -t 0 ]; then
    read -r -p 'Press Enter to close ' _ || true
  fi
  exit "$result"
}
trap finish EXIT
fail() { printf '\n%s\n' "$1" >&2; exit 1; }
printf '\nSpooldesk installer | macOS / Linux\nInstallation folder: %s/spooldesk\n\n' "$bundle"
case "$bundle" in *,*|*$'\n'*) fail 'Extract the ZIP to a folder without commas or newlines in its path.' ;; esac
[ -f SHA256SUMS ] || fail 'Extract the whole installation ZIP first, then open the launcher.'
printf '[1/6] Checking package integrity...\n'
[ ! -L installer ] && [ ! -L package ] || fail 'Linked package folders are not supported.'
expected_names='package/spooldesk-image.tar.gz package/spooldesk-image-arm64.tar.gz package/spooldesk-source.zip package/spooldesk-source.tar.gz package/IMAGE_ID package/IMAGE_TAG package/IMAGE_ID_ARM64 package/IMAGE_TAG_ARM64 package/IMAGE_IDS package/IMAGE_IDS_ARM64 package/SOURCE_COMMIT package/VALIDATION.json README.txt INSTALLATION.md INSTALLATION_RU.md TROUBLESHOOTING.md INSTALL-WINDOWS.cmd INSTALL-MACOS.command INSTALL-LINUX.sh installer/configure.py installer/install.ps1 installer/install.sh'
seen=' '
count=0
while IFS= read -r line || [ -n "$line" ]; do
  expected=${line%%  *}
  name=${line#*  }
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid checksum manifest.'
  case " $expected_names " in *" $name "*) ;; *) fail 'Unexpected checksum entry.' ;; esac
  case "$seen" in *" $name "*) fail 'Duplicate checksum entry.' ;; esac
  [ -f "$name" ] && [ ! -L "$name" ] || fail 'Missing or linked package file.'
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$name")
  else
    actual=$(shasum -a 256 "$name")
  fi
  [ "${actual%% *}" = "$expected" ] || fail "Package checksum mismatch: $name. Download a fresh ZIP."
  seen="$seen$name "
  count=$((count + 1))
done < SHA256SUMS
[ "$count" -eq 22 ] || fail 'Incomplete checksum manifest.'
printf '[2/6] Checking Docker...\n'
command -v docker >/dev/null 2>&1 || fail 'Install Docker Desktop (macOS) or Docker Engine with Compose v2 (Linux), then run this file again. See INSTALLATION.md. Python and Git are not required.'
endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
case "$endpoint" in unix://*) ;; *) fail 'This installer requires a local Docker engine, not a remote context.' ;; esac
docker_info=$(docker info --format '{{.OSType}} {{.Architecture}}') || fail 'Start Docker Desktop or the Docker service, then run this file again.'
case "$docker_info" in
  'linux x86_64'|'linux amd64') arch=amd64; suffix=''; image_file=spooldesk-image.tar.gz ;;
  'linux aarch64'|'linux arm64') arch=arm64; suffix=_ARM64; image_file=spooldesk-image-arm64.tar.gz ;;
  *) fail 'A Linux Docker engine (amd64 or arm64) is required.' ;;
esac
docker compose version
unset POSTGRES_PASSWORD POSTGRES_DB POSTGRES_USER SPOOLDESK_IMAGE SPOOLDESK_ENV_FILE SPOOLDESK_PORT SPOOLDESK_INSTALLATION_ID COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES DOCKER_DEFAULT_PLATFORM
image=$(cat "package/IMAGE_TAG$suffix")
expected_id=$(cat "package/IMAGE_ID$suffix")
[[ "$image" =~ ^spooldesk:rc-[0-9a-f]{40}-(amd64|arm64)$ ]] || fail 'Invalid image tag.'
[[ "$expected_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Invalid image identity.'
# Both digests are derived from the same checked archive during packaging.
# Classic Docker reports config ID; containerd reports the OCI manifest ID.
accepted_ids=' '
id_count=0
while IFS= read -r accepted_id || [ -n "$accepted_id" ]; do
  [[ "$accepted_id" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'Invalid accepted image identity.'
  accepted_ids="$accepted_ids$accepted_id "
  id_count=$((id_count + 1))
done < "package/IMAGE_IDS$suffix"
[ "$id_count" -eq 2 ] || fail 'Invalid accepted image identities.'
case "$accepted_ids" in *" $expected_id "*) ;; *) fail 'Tested image ID is missing from accepted identities.' ;; esac
printf '[3/6] Loading the tested %s image...\n' "$arch"
docker load --input "package/$image_file"
loaded_id=$(docker image inspect "$image" --format '{{.Id}}')
loaded_platform=$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
case "$accepted_ids" in *" $loaded_id "*) ;; *) fail "Loaded image differs from the tested image. Actual: $loaded_id; expected one of:$accepted_ids" ;; esac
[ "$loaded_platform" = "linux/$arch" ] || fail "Loaded image differs from the tested image platform: $loaded_platform. Expected linux/$arch."
# Atomic engine-wide lock; cleanup removes only the ID acquired by this process.
lock_container=$(docker create --name spooldesk-installer-lock --network none --label io.spooldesk.installer=1 --entrypoint python "$image" -c pass) || fail 'Another installer may be running. If it was interrupted, remove only spooldesk-installer-lock in Docker Desktop, then retry.'
project_dir="$bundle/spooldesk"
containers=$(docker ps -aq --filter label=com.docker.compose.project=spooldesk)
volumes=$(docker volume ls -q --filter label=com.docker.compose.project=spooldesk)
if [ -n "$containers$volumes" ]; then
  [ -f "$project_dir/INSTALLATION_ID" ] || fail 'An existing spooldesk installation was found. Nothing changed. Follow the upgrade/backup procedure instead of creating another database.'
  install_id=$(cat "$project_dir/INSTALLATION_ID")
  [[ "$install_id" =~ ^[0-9a-f]{32}$ ]] || fail 'Invalid installation marker.'
  for volume in $volumes; do
    owner=$(docker volume inspect "$volume" --format '{{index .Labels "io.spooldesk.installation"}}')
    [ "$owner" = "$install_id" ] || fail 'Existing storage belongs to another installation. Nothing changed.'
  done
  for container in $containers; do
    directory=$(docker inspect "$container" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')
    [ "$directory" = "$project_dir" ] || fail 'Existing containers belong to another folder. Open the installer in that original folder.'
  done
fi
printf '[4/6] Configure your installation...\n'
terminal=(-i)
if [ -t 0 ] && [ -t 1 ]; then terminal=(-it); fi
configuration_result=0
docker run --rm "${terminal[@]}" --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e PYTHONUTF8=1 -e PYTHONIOENCODING=utf-8 \
  --user "$(id -u):$(id -g)" --mount "type=bind,source=$bundle,target=/bundle" --entrypoint python \
  "$image" /bundle/installer/configure.py --arch "$arch" || configuration_result=$?
[ "$configuration_result" -ne 3 ] || exit 0
[ "$configuration_result" -eq 0 ] || fail 'Configuration did not finish. Check the message above and run this file again.'
cd "$project_dir"
compose=(compose --project-directory "$project_dir" --env-file "$project_dir/.env" -f "$project_dir/docker-compose.yml" -p spooldesk)
printf '[5/6] Starting PostgreSQL and Spooldesk...\n'
if [ -z "$containers" ]; then
  url=$(cat OPEN_URL)
  [[ "$url" =~ ^http://localhost:[0-9]{4,5}$ ]] || fail 'Invalid dashboard address.'
  port=${url##*:}
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    fail "Port $port is unavailable. Stop the application using it, then run this file again. Saved settings are preserved. See TROUBLESHOOTING.md."
  fi
fi
if [ -z "$containers$volumes" ]; then docker pull postgres:16-alpine; fi
docker "${compose[@]}" up -d --no-build --no-recreate --pull never dashboard
printf '[6/6] Waiting for the dashboard...\n'
container_id=$(docker "${compose[@]}" ps -q dashboard)
healthy=0
for ((attempt=0; attempt<90; attempt++)); do
  health=$(docker inspect "$container_id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
  if [ "$health" = healthy ]; then healthy=1; break; fi
  if [ "$health" = unhealthy ] || [ "$health" = none ]; then break; fi
  sleep 2
done
[ "$healthy" -eq 1 ] || fail 'Dashboard did not become healthy. Keep this folder; inspect dashboard/postgres logs in Docker. Run this file again after resolving the error.'
url=$(cat OPEN_URL)
[[ "$url" =~ ^http://localhost:[0-9]{4,5}$ ]] || fail 'Invalid dashboard address.'
printf '\nReady: %s\nLogin: admin@local\nUse the password you saved. Run this same file next time; keep this installation folder.\n\n' "$url"
if [ "${SPOOLDESK_INSTALLER_NO_BROWSER:-}" != 1 ]; then
  if command -v open >/dev/null 2>&1 && [ "$(uname -s)" = Darwin ]; then open "$url" || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
  fi
fi
