"""Wrap verified CI files and launchers into a portable installation ZIP."""
from pathlib import Path
import argparse
import hashlib
import re
import shutil
import zipfile

if __package__:
    from .image_archive_identity import image_ids
else:
    from image_archive_identity import image_ids

ROOT = Path(__file__).resolve().parents[1]
BASE_FILES = ('spooldesk-image.tar.gz', 'spooldesk-image-arm64.tar.gz', 'spooldesk-source.zip',
              'spooldesk-source.tar.gz', 'IMAGE_ID', 'IMAGE_TAG', 'IMAGE_ID_ARM64', 'IMAGE_TAG_ARM64',
              'SOURCE_COMMIT', 'INSTALL-RU.md', 'VALIDATION.json')
LAUNCHERS = ('INSTALL-WINDOWS.cmd', 'INSTALL-MAC.command', 'INSTALL-LINUX.sh')
HELPERS = ('configure.py', 'install.ps1', 'install.sh')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def package(candidate: Path, version: str, source_root: Path = ROOT) -> Path:
    if not re.fullmatch(r'v\d+\.\d+\.\d+-rc\.\d+', version):
        raise ValueError('Expected a release-candidate version')
    if candidate.is_symlink():
        raise ValueError('Linked candidate directory')
    checks = {}
    for line in (candidate / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        checksum, name = line.split('  ', 1)
        if name not in BASE_FILES or name in checks:
            raise ValueError('Unexpected input manifest entry')
        file = candidate / name
        if file.is_symlink() or not file.is_file() or digest(file) != checksum:
            raise ValueError(f'Candidate checksum mismatch: {name}')
        checks[name] = checksum
    if set(checks) != set(BASE_FILES):
        raise ValueError('Incomplete candidate')
    archive = candidate / f'spooldesk-{version}-transfer.zip'
    if archive.exists() or (candidate / 'installer').exists() or (candidate / 'IMAGE_IDS').exists():
        raise ValueError('Refuse to replace an existing installer package')
    identities = {}
    for arch, suffix, filename in [('amd64', '', 'spooldesk-image.tar.gz'),
                                   ('arm64', '_ARM64', 'spooldesk-image-arm64.tar.gz')]:
        identities[suffix] = image_ids(candidate / filename,
            (candidate / ('IMAGE_TAG' + suffix)).read_text().strip(),
            (candidate / ('IMAGE_ID' + suffix)).read_text().strip(), arch)
    for suffix, ids in identities.items():
        relative = 'IMAGE_IDS' + suffix
        (candidate / relative).write_text(ids['config'] + '\n' + ids['manifest'] + '\n', encoding='ascii')
        checks[relative] = digest(candidate / relative)
    (candidate / 'installer').mkdir()
    for name in LAUNCHERS + HELPERS:
        source = source_root / 'scripts' / 'installer' / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing or linked launcher source')
        relative = name if name in LAUNCHERS else 'installer/' + name
        shutil.copyfile(source, candidate / relative)
        checks[relative] = digest(candidate / relative)
    manifest = ''.join(f'{checks[name]}  {name}\n' for name in sorted(checks))
    (candidate / 'SHA256SUMS').write_text(manifest, encoding='ascii')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted([*checks, 'SHA256SUMS']):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o100755 if name.endswith(('.sh', '.command')) else 0o100644
            info.external_attr = mode << 16
            with (candidate / name).open('rb') as incoming, bundle.open(info, 'w', force_zip64=True) as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    archive.with_suffix('.zip.sha256').write_text(f'{digest(archive)}  {archive.name}\n', encoding='ascii')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(package(args.candidate, args.version))
