"""Fake Docker CLI used only by isolated launcher tests, on each native OS shell."""
from pathlib import Path
import json
import os
import sys

root = Path(os.environ['INSTALLER_TEST_BUNDLE'])
sys.path.insert(0, os.environ['INSTALLER_TEST_REPO'])
from scripts.installer import configure

args = sys.argv[1:]
with (root / 'mock-calls.jsonl').open('a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\n')
active = (root / 'mock-active').exists()
foreign = os.environ.get('INSTALLER_TEST_FOREIGN') == '1'
target = root / 'spooldesk'
if args[:2] == ['context', 'inspect']:
    print('npipe://local' if os.name == 'nt' else 'unix:///local')
elif args[0] == 'info':
    print('linux amd64')
elif args[0] == 'ps':
    if active or foreign: print('test-container')
elif args[:2] == ['volume', 'ls']:
    if active or foreign: print('test-volume')
elif args[:2] == ['volume', 'inspect']:
    owner = (target / 'INSTALLATION_ID').read_text().strip() if active else 'foreign'
    print(json.dumps({'io.spooldesk.installation': owner}) if 'json' in args[-1] else owner)
elif args[:2] == ['image', 'inspect']:
    if 'Architecture' in args[-1]:
        print(os.environ.get('INSTALLER_TEST_IMAGE_PLATFORM', 'linux/amd64'))
    elif os.environ.get('INSTALLER_TEST_IMAGE_STORE') == 'containerd':
        print((root / 'IMAGE_IDS').read_text().splitlines()[1])
    elif os.environ.get('INSTALLER_TEST_IMAGE_STORE') == 'unknown':
        print('sha256:' + 'd' * 64)
    else:
        print((root / 'IMAGE_ID').read_text().strip())
elif args[0] == 'load':
    pass
elif args[0] == 'create':
    if os.environ.get('INSTALLER_TEST_LOCKED') == '1': sys.exit(1)
    (root / 'mock-lock').write_text('1')
    print('installer-test-lock')
elif args[0] == 'rm':
    assert args[1:] == ['installer-test-lock']
    (root / 'mock-lock').unlink()
elif args[0] == 'run':
    identity = configure.image_identity(root, 'amd64')
    if target.exists():
        configure.validate_existing(target, identity)
    else:
        configure.create_installation(root, identity, 8000, 'installer-test-password', 0)
elif args[0] == 'pull':
    if os.environ.get('INSTALLER_TEST_FAIL_PULL') == '1': sys.exit(1)
elif args[0] == 'compose':
    if args[1] == 'version': print('Docker Compose version v2.39.0')
    elif 'up' in args:
        assert 'POSTGRES_PASSWORD' not in os.environ and 'SPOOLDESK_IMAGE' not in os.environ
        (root / 'mock-active').write_text('1')
    elif 'ps' in args: print('test-container')
    else: raise RuntimeError('Unexpected mock Compose operation')
elif args[0] == 'inspect':
    if 'Health' in args[-1]: print('healthy')
    else:
        print(json.dumps({'com.docker.compose.project.working_dir': str(target)}) if 'json' in args[-1] else str(target))
else:
    raise RuntimeError('Unexpected mock Docker operation')
