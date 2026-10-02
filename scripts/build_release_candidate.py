"""Create an auditable LOCAL working-tree candidate; never commit or publish it.

Build/test the source/ snapshot, not the changing checkout. The manifest identifies
every included byte; base_commit alone does not identify an uncommitted snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {'src', 'tests', 'scripts', 'docs', 'assets', 'agents', '.github'}
TOP_LEVEL = {'Dockerfile', 'Makefile', 'Caddyfile', 'LICENSE', 'pytest.ini', 'tailwind.config.js',
             '.dockerignore', '.gitignore', '.gitattributes', '.env.example'}
TEXT = {'.py', '.js', '.cjs', '.html', '.css', '.json', '.webmanifest', '.md', '.txt', '.in', '.yml', '.yaml', '.ini', '.sh', '.example', '.toml'}
ASSETS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.woff', '.woff2', '.ttf'}
SENSITIVE = re.compile(rb'(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
    rb'\bsk-or-v1-[A-Za-z0-9]{32,}|\bsk-ant-api\d+-[A-Za-z0-9_-]{32,}|'
    rb'\bxox[baprs]-[0-9]{8,}-[0-9A-Za-z-]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}')


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {'..', '.', '__pycache__', 'node_modules'} for p in path.parts):
        return False
    if path.name.startswith('.env') and name != '.env.example':
        return False
    if path.name == 'secrets.key' or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.key', '.pem', '.log', '.dump', '.bak', '.zip'}:
        return False
    if name.startswith('agents/tasks/'):
        return name == 'agents/tasks/orchestration-smoke.json'
    if len(path.parts) == 1:
        return (name in TOP_LEVEL or name.startswith('requirements') and path.suffix in {'.in', '.txt'}
                or name.startswith('docker-compose') and path.suffix == '.yml'
                or path.suffix == '.md' and name != 'MANUAL_STEPS.md')
    return path.parts[0] in DIRECTORIES and path.suffix.lower() in TEXT | ASSETS


def safe_bytes(root: Path, rel: str) -> bytes:
    path = root / rel
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Outside repository: {rel}')
    for part in (path, *path.parents):
        if part == root: break
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError(f'Linked path refused: {rel}')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f'Not a regular independent file: {rel}')
    raw = path.read_bytes()
    if path.suffix in TEXT or path.name in TOP_LEVEL:
        if SENSITIVE.search(raw):
            raise ValueError(f'Possible credential in {rel}; inspect locally (value withheld)')
    return raw


def build(output: Path, version: str, root: Path = ROOT) -> dict:
    if not re.fullmatch(r'v\d+\.\d+\.\d+-rc\.\d+(?:-[a-z0-9-]+)?', version):
        raise ValueError('Use a release-candidate version such as v0.1.0-rc.1')
    names = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'], cwd=root).decode('utf-8').split('\0')
    selected = sorted({name for name in names if name and allowed(name) and (root/name).is_file()})
    if not {'Dockerfile','requirements.txt','src/dashboard/app.py'}.issubset(selected):
        raise ValueError('Incomplete source selection')
    # Read and inspect everything before creating an output directory.
    files = {name:safe_bytes(root,name) for name in selected}
    base = subprocess.check_output(['git','rev-parse','HEAD'], cwd=root, text=True).strip()
    hashes = {name:hashlib.sha256(raw).hexdigest() for name,raw in files.items()}
    digest = hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    source = output/'source'
    for name, raw in files.items():
        target = source/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(raw)
    manifest = {'version':version, 'kind':'local-working-tree-candidate', 'base_commit':base,
        'source_digest':digest, 'file_count':len(files), 'files':hashes,
        'limitations':['Not a Git tag or published release.', 'Passing tests and image identity must be recorded separately.',
                        'Secret pattern scanning supplements, and does not replace, human source review.']}
    (output/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    (output/'SOURCE_BASE_COMMIT').write_text(base+'\n',encoding='ascii')
    with zipfile.ZipFile(output/f'spooldesk-{version}-source.zip','x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            member=zipfile.ZipInfo(f'spooldesk/{name}',date_time=(2026,1,1,0,0,0))
            member.external_attr=(0o100755 if name.endswith('.sh') else 0o100644)<<16
            member.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(member,raw)
    print(json.dumps({k:v for k,v in manifest.items() if k != 'files'},indent=2))
    return manifest


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--version',required=True)
    args=parser.parse_args()
    build(args.output,args.version)
