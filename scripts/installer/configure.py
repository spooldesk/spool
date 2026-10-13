"""Offline configuration wizard, executed by the verified release image.

Only the extracted release bundle is mounted, at /bundle. No Docker socket,
network, host Python, application import, existing database or external API.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tempfile
import zipfile

PASSWORD = re.compile(r"[A-Za-z0-9!@#%^&*()_+=.,:?-]{12,128}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
TEXT = {
    'port': ('Dashboard port [8000]', 'Порт кабинета [8000]'),
    'bad_port': ('Use 1024–65535, except 8011 (reserved for the developer preview).',
                 'Укажи 1024–65535, кроме 8011 (порт рабочей демосреды).'),
    'password': ('Owner password (Enter = generate)', 'Пароль владельца (Enter = сгенерировать)'),
    'confirm': ('Repeat password', 'Повтори пароль'),
    'bad_password': ('Use 12–128 ASCII letters, digits or !@#%^&*()_+=.,:?-; passwords must match.',
                     'Нужно 12–128 латинских букв, цифр или !@#%^&*()_+=.,:?-; пароли должны совпадать.'),
    'save': ('Save this password now. It will not be displayed again.',
             'Сохрани пароль сейчас. При следующем запуске он не будет показан.'),
    'install': ('Install here using these settings? [Y/n]', 'Установить с этими настройками? [Д/н]'),
    'cancel': ('Cancelled. No installation was created.', 'Отменено. Установка не создана.'),
    'created': ('Settings saved. The launcher will start PostgreSQL and Spooldesk.',
                'Настройки сохранены. Сейчас запустятся PostgreSQL и Spooldesk.'),
    'resume': ('Existing installation verified. Reusing its password, keys and storage.',
               'Существующая установка проверена. Пароль, ключи и данные сохраняются.'),
}


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f'Expected an independent regular file: {path.name}')


def image_identity(root: Path, arch: str) -> dict:
    regular(root / 'VALIDATION.json')
    data = json.loads((root / 'VALIDATION.json').read_text(encoding='utf-8'))
    commit = data.get('source_commit', '')
    if not COMMIT.fullmatch(commit) or arch not in ('amd64', 'arm64'):
        raise ValueError('Invalid release identity')
    selected = data['images']['linux/' + arch]
    tag = f'spooldesk:rc-{commit}-{arch}'
    if selected['image_tag'] != tag or not re.fullmatch(r'sha256:[0-9a-f]{64}', selected['image_id']):
        raise ValueError('Invalid image identity')
    suffix = '_ARM64' if arch == 'arm64' else ''
    for name, expected in [('IMAGE_TAG' + suffix, tag), ('IMAGE_ID' + suffix, selected['image_id']),
                           ('SOURCE_COMMIT', commit)]:
        regular(root / name)
        if (root / name).read_text().strip() != expected:
            raise ValueError('Release identity files disagree')
    return {'source_commit': commit, 'image_tag': tag, 'image_id': selected['image_id'], 'arch': arch}


def extract_source(archive: Path, destination: Path) -> None:
    """Validate the entire ZIP before writing; reject links and portable path tricks."""
    regular(archive)
    with zipfile.ZipFile(archive) as source:
        seen = set()
        total = 0
        selected = []
        for entry in source.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (not entry.filename.startswith('spooldesk/') or path.is_absolute()
                    or '\\' in entry.filename or any(p in ('..', '.') for p in entry.filename.split('/'))
                    or any(':' in p or p.endswith((' ', '.')) for p in path.parts)
                    or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError('Unsafe source archive member')
            relative = Path(*path.parts[1:])
            if relative == Path('.'):
                if entry.is_dir():
                    continue
                raise ValueError('Invalid source archive root')
            folded = relative.as_posix().casefold()
            if folded in seen or any(part.split('.')[0].upper() in
                                     {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                                      *(f'LPT{i}' for i in range(1, 10))} for part in relative.parts):
                raise ValueError('Duplicate or reserved source path')
            seen.add(folded)
            if relative.name == '.env' or (relative.name.startswith('.env.') and relative.name != '.env.example'):
                raise ValueError('Private configuration in source archive')
            total += entry.file_size
            if total > 200 * 1024 * 1024 or len(seen) > 10000:
                raise ValueError('Source archive exceeds installation limits')
            selected.append((entry, relative, mode))
        for entry, relative, mode in selected:
            target = destination / relative
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod(0o755 if mode & 0o111 else 0o644)
    if not (destination / 'docker-compose.yml').is_file():
        raise ValueError('Source archive has no Compose configuration')


def validate_existing(target: Path, identity: dict) -> dict:
    if target.is_symlink() or not target.is_dir():
        raise ValueError('Installation directory is not a regular directory')
    for name in ('INSTALLATION.json', 'INSTALLATION_ID', 'OPEN_URL', '.env', 'docker-compose.yml'):
        regular(target / name)
    state = json.loads((target / 'INSTALLATION.json').read_text(encoding='utf-8'))
    if state.get('format') != 1 or any(state.get(k) != v for k, v in identity.items()):
        raise ValueError('Different or unsupported installation. Use the upgrade/backup procedure.')
    if not IDENTITY.fullmatch(state.get('installation_id', '')) or state.get('project') != 'spooldesk':
        raise ValueError('Invalid installation marker')
    port = state.get('port')
    if type(port) is not int or not 1024 <= port <= 65535 or port == 8011:
        raise ValueError('Invalid saved port')
    if (sha(target / '.env') != state.get('env_sha256')
            or sha(target / 'docker-compose.yml') != state.get('compose_sha256')
            or (target / 'INSTALLATION_ID').read_text().strip() != state['installation_id']
            or (target / 'OPEN_URL').read_text().strip() != f'http://localhost:{port}'):
        raise ValueError('Installation configuration changed. Nothing overwritten; inspect it before restarting.')
    return state


def create_installation(root: Path, identity: dict, port: int, password: str, language: int) -> dict:
    target = root / 'spooldesk'
    if target.exists() or target.is_symlink():
        raise ValueError('The spooldesk directory already exists. Nothing overwritten.')
    if not PASSWORD.fullmatch(password) or not 1024 <= port <= 65535 or port == 8011:
        raise ValueError('Invalid installation settings')
    # All generated values are single-quote-safe and contain no Compose interpolation.
    installation_id = secrets.token_hex(16)
    config = {
        'DATABASE_URL': '', 'BOT_TOKEN': '', 'ADMIN_CHAT_ID': '', 'GEMINI_API_KEY': '',
        'POSTGRES_USER': 'spooldesk', 'POSTGRES_DB': 'spooldesk',
        'POSTGRES_PASSWORD': secrets.token_urlsafe(32), 'ACCESS_PASSWORD': password,
        'DASH_SECRET_KEY': secrets.token_urlsafe(48), 'SECRETS_KEY': secrets.token_urlsafe(48),
        'DASH_COOKIE_SECURE': 'false', 'LOG_LEVEL': 'INFO',
        'SPOOLDESK_IMAGE': identity['image_tag'], 'SPOOLDESK_PORT': str(port),
        'SPOOLDESK_INSTALLATION_ID': installation_id,
    }
    with tempfile.TemporaryDirectory(prefix='.spooldesk-setup-', dir=root) as staging_name:
        staging = Path(staging_name)
        extract_source(root / 'spooldesk-source.zip', staging)
        env = staging / '.env'
        with open(env, 'x', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
            output.write('# Generated by Spooldesk installer. Keep private; back up SECRETS_KEY separately.\n')
            output.writelines(f"{key}='{value}'\n" for key, value in config.items())
        state = {'format': 1, **identity, 'installation_id': installation_id, 'project': 'spooldesk',
                 'port': port, 'language': language, 'env_sha256': sha(env),
                 'compose_sha256': sha(staging / 'docker-compose.yml')}
        (staging / 'INSTALLATION.json').write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
        (staging / 'INSTALLATION_ID').write_text(installation_id + '\n', encoding='ascii')
        (staging / 'OPEN_URL').write_text(f'http://localhost:{port}\n', encoding='ascii')
        # mkdir reserves the name even if another installer appeared during extraction.
        target.mkdir()
        try:
            for child in staging.iterdir():
                child.rename(target / child.name)
        except BaseException:
            # Retain partial configuration for inspection; never replace or delete it.
            raise
    return state


def main(root: Path, arch: str) -> int:
    root = root.resolve()
    identity = image_identity(root, arch)
    target = root / 'spooldesk'
    if target.exists() or target.is_symlink():
        state = validate_existing(target, identity)
        language = 1 if state.get('language') == 1 else 0
        print(TEXT['resume'][language])
        return 0
    language = 1 if input('Language / Язык: 1 English, 2 Русский [1]: ').strip() == '2' else 0
    def say(key):
        print(TEXT[key][language])
    while True:
        raw = input(TEXT['port'][language] + ': ').strip() or '8000'
        if raw.isascii() and raw.isdigit() and 1024 <= int(raw) <= 65535 and int(raw) != 8011:
            port = int(raw)
            break
        say('bad_port')
    while True:
        password = getpass.getpass(TEXT['password'][language] + ': ')
        if not password:
            password = secrets.token_urlsafe(18)
            break
        if PASSWORD.fullmatch(password) and getpass.getpass(TEXT['confirm'][language] + ': ') == password:
            break
        say('bad_password')
    print('\nLogin: admin@local')
    print('Password: ' + password)
    print(f'URL: http://localhost:{port}')
    say('save')
    if input(TEXT['install'][language] + ': ').strip().casefold() not in ('', 'y', 'yes', 'д', 'да'):
        say('cancel')
        return 3
    create_installation(root, identity, port, password, language)
    say('created')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/bundle'))
    parser.add_argument('--arch', choices=['amd64', 'arm64'], required=True)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.root, args.arch))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f'Installation stopped: {error}', flush=True)
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled. Existing data was not removed.', flush=True)
        raise SystemExit(3)
