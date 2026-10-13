"""Stdlib-only installer tests. No app imports, live Docker, databases or model calls."""
from pathlib import Path
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from scripts.installer import configure
from scripts.package_install_bundle import BASE_FILES, package
from scripts.image_archive_identity import image_ids

REPO = Path(__file__).resolve().parents[2]
COMMIT = 'b' * 40
PASSWORD = 'installer-test-password'


def image_fixture(path, tag, arch):
    config = json.dumps({'os': 'linux', 'architecture': arch, 'rootfs': {'type': 'layers', 'diff_ids': []}}).encode()
    config_id = 'sha256:' + hashlib.sha256(config).hexdigest()
    config_path = 'blobs/sha256/' + config_id.split(':')[1]
    manifest = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                           'config': {'mediaType': 'application/vnd.oci.image.config.v1+json',
                                      'digest': config_id, 'size': len(config)}, 'layers': []}).encode()
    manifest_id = 'sha256:' + hashlib.sha256(manifest).hexdigest()
    metadata = {
        'index.json': json.dumps({'schemaVersion': 2, 'manifests': [{
            'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'digest': manifest_id, 'size': len(manifest)}]}).encode(),
        'manifest.json': json.dumps([{'Config': config_path, 'RepoTags': [tag], 'Layers': []}]).encode(),
        config_path: config, 'blobs/sha256/' + manifest_id.split(':')[1]: manifest}
    write_image(path, metadata)
    return config_id, manifest_id


def write_image(path, metadata):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in metadata.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def fixture(root):
    root.mkdir(parents=True)
    images = {}
    for arch, suffix in [('amd64', ''), ('arm64', '_ARM64')]:
        tag = f'spooldesk:rc-{COMMIT}-{arch}'
        filename = 'spooldesk-image.tar.gz' if arch == 'amd64' else 'spooldesk-image-arm64.tar.gz'
        config_id, _ = image_fixture(root / filename, tag, arch)
        (root / ('IMAGE_TAG' + suffix)).write_text(tag + '\n')
        (root / ('IMAGE_ID' + suffix)).write_text(config_id + '\n')
        images['linux/' + arch] = {'image_tag': tag, 'image_id': config_id}
    (root / 'SOURCE_COMMIT').write_text(COMMIT + '\n')
    (root / 'VALIDATION.json').write_text(json.dumps({'source_commit': COMMIT, 'images': images}))
    with zipfile.ZipFile(root / 'spooldesk-source.zip', 'w') as archive:
        archive.writestr('spooldesk/docker-compose.yml', 'services: {}\n')
        archive.writestr('spooldesk/docs/readme.md', 'Test source\n')
    for name in BASE_FILES:
        if not (root / name).exists():
            (root / name).write_bytes(b'fixture only\n')
    (root / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256((root/name).read_bytes()).hexdigest()}  {name}\n' for name in BASE_FILES))
    return root


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = fixture(Path(self.temp.name) / 'release bundle тест').resolve()
        self.archive = package(self.root, 'v0.1.0-rc.9')
        self.identity = configure.image_identity(self.root, 'amd64')

    def test_fresh_and_resume_preserve_independent_secrets(self):
        configure.create_installation(self.root, self.identity, 8123, PASSWORD, 0)
        target = self.root / 'spooldesk'
        original = (target / '.env').read_bytes()
        state = configure.validate_existing(target, self.identity)
        self.assertEqual(state['port'], 8123)
        self.assertEqual((target / '.env').read_bytes(), original)
        values = dict(line.split('=', 1) for line in original.decode().splitlines() if '=' in line)
        secrets = [values[k] for k in ('POSTGRES_PASSWORD', 'SECRETS_KEY', 'DASH_SECRET_KEY', 'ACCESS_PASSWORD')]
        self.assertEqual(len(set(secrets)), 4)
        self.assertEqual(values['SPOOLDESK_IMAGE'], "'" + self.identity['image_tag'] + "'")
        self.assertNotIn(PASSWORD, (target / 'INSTALLATION.json').read_text())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE((target / '.env').stat().st_mode), 0o600)

    def test_different_release_or_modified_config_refused(self):
        configure.create_installation(self.root, self.identity, 8000, PASSWORD, 0)
        target = self.root / 'spooldesk'
        with self.assertRaises(ValueError):
            configure.validate_existing(target, {**self.identity, 'source_commit': 'c' * 40})
        (target / '.env').write_text('do not replace\n')
        with self.assertRaises(ValueError):
            configure.validate_existing(target, self.identity)
        self.assertEqual((target / '.env').read_text(), 'do not replace\n')

    def test_existing_directory_and_cancellation_preserved(self):
        with patch('builtins.input', side_effect=['1', '', 'n']), patch('scripts.installer.configure.read_password', return_value=PASSWORD):
            # Custom passwords require confirmation before the final install prompt.
            self.assertEqual(configure.main(self.root, 'amd64'), 3)
        self.assertFalse((self.root / 'spooldesk').exists())
        (self.root / 'spooldesk').mkdir()
        (self.root / 'spooldesk' / 'keep').write_text('retain')
        with self.assertRaises(ValueError):
            configure.create_installation(self.root, self.identity, 8000, PASSWORD, 0)
        self.assertEqual((self.root / 'spooldesk' / 'keep').read_text(), 'retain')

    def test_confirmation_accepts_numeric_default_and_localized_answers(self):
        for answer in ('', '1', '  Д  ', 'да', 'YES', 'так', '１'):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer):
                self.assertTrue(configure.confirm_install(1))
        for answer in ('0', 'n', ' НЕТ ', 'ні'):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer):
                self.assertFalse(configure.confirm_install(1))
        with patch('builtins.input', side_effect=['garbled', '?', '1']), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertTrue(configure.confirm_install(1))
        self.assertEqual(out.getvalue().count(configure.TEXT['bad_choice'][1]), 2)

    def test_generated_password_displayed_once_and_saved(self):
        generated = 'GeneratedTestValue123'
        with patch('builtins.input', side_effect=['1', '', '']), patch('scripts.installer.configure.read_password', return_value=''), \
                patch('secrets.token_urlsafe', side_effect=lambda n: generated if n == 18 else 'x' * n), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(configure.main(self.root, 'amd64'), 0)
        self.assertEqual(out.getvalue().count(generated), 1)
        self.assertIn(generated, (self.root / 'spooldesk/.env').read_text())
        with patch('builtins.input', side_effect=AssertionError('Resume must not prompt')), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(configure.main(self.root, 'amd64'), 0)
        self.assertNotIn(generated, out.getvalue())

    def test_password_interpolation_and_reserved_port_rejected(self):
        for password in ('short', 'unsafe$ENV-password', "quote'password-long", 'line\nbreak-password'):
            with self.subTest(password=password), self.assertRaises(ValueError):
                configure.create_installation(self.root, self.identity, 8000, password, 0)
        for port in (80, 8011, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                configure.create_installation(self.root, self.identity, port, PASSWORD, 0)

    def test_zip_traversal_links_and_case_collisions_rejected_before_writes(self):
        cases = [('spooldesk/../../outside', None), ('/absolute', None),
                 ('spooldesk/a:stream', None), ('spooldesk/NUL.txt', None),
                 ('spooldesk/link', stat.S_IFLNK | 0o777)]
        for index, (name, mode) in enumerate(cases):
            archive = self.root / f'bad-{index}.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('spooldesk/safe.txt', 'not written')
                member = zipfile.ZipInfo(name)
                if mode: member.external_attr = mode << 16
                output.writestr(member, 'bad')
            destination = self.root / f'extract-{index}'
            destination.mkdir()
            with self.subTest(name=name), self.assertRaises(ValueError):
                configure.extract_source(archive, destination)
            self.assertEqual(list(destination.iterdir()), [])
        with zipfile.ZipFile(self.root / 'case.zip', 'w') as output:
            output.writestr('spooldesk/File', 'one')
            output.writestr('spooldesk/file', 'two')
        with self.assertRaises(ValueError):
            configure.extract_source(self.root / 'case.zip', self.root)

    def test_bundle_contains_launchers_with_executable_permissions(self):
        archive = self.archive
        with zipfile.ZipFile(archive) as contents:
            self.assertEqual(len(contents.namelist()), 23)
            for name in ('INSTALL-LINUX.sh', 'INSTALL-MACOS.command', 'installer/install.sh'):
                self.assertEqual(stat.S_IMODE(contents.getinfo(name).external_attr >> 16), 0o755)
            self.assertEqual(contents.read('package/spooldesk-source.zip'), (self.root / 'spooldesk-source.zip').read_bytes())
        self.assertEqual(len((self.root / 'SHA256SUMS').read_text().splitlines()), 22)
        with self.assertRaises(ValueError):
            package(self.root, 'v0.1.0-rc.7')

    def test_archive_links_classic_and_containerd_ids(self):
        path = self.root / 'spooldesk-image.tar.gz'
        ids = image_ids(path, self.identity['image_tag'], self.identity['image_id'], 'amd64')
        self.assertEqual(ids['config'], self.identity['image_id'])
        self.assertNotEqual(ids['config'], ids['manifest'])
        self.assertEqual(image_ids(path, self.identity['image_tag'], ids['manifest'], 'amd64'), ids)
        for tag, identity, arch in [(self.identity['image_tag'], 'sha256:' + 'd' * 64, 'amd64'),
                                    (self.identity['image_tag'], ids['config'], 'arm64'),
                                    ('other:tag', ids['config'], 'amd64')]:
            with self.assertRaises(ValueError):
                image_ids(path, tag, identity, arch)

    def test_archive_unlinked_or_modified_metadata_rejected(self):
        path = self.root / 'spooldesk-image.tar.gz'
        with tarfile.open(path, 'r:gz') as archive:
            original = {p.name: archive.extractfile(p).read() for p in archive.getmembers()}
        config_path = json.loads(original['manifest.json'])[0]['Config']
        bad_index = json.loads(original['index.json'])
        bad_index['manifests'].append(bad_index['manifests'][0])
        bad_legacy = json.loads(original['manifest.json'])
        bad_legacy[0]['Config'] = 'blobs/sha256/' + 'e' * 64
        cases = [{**original, config_path: original[config_path] + b' '},
                 {**original, 'index.json': json.dumps(bad_index).encode()},
                 {**original, 'manifest.json': json.dumps(bad_legacy).encode()}]
        for metadata in cases:
            write_image(path, metadata)
            with self.assertRaises(ValueError):
                image_ids(path, self.identity['image_tag'], self.identity['image_id'], 'amd64')


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = fixture(Path(self.temp.name) / 'release bundle тест').resolve()
        package(self.root, 'v0.1.0-rc.7')
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        mock = self.bin / 'docker_mock.py'
        mock.write_text((REPO / 'tests/installer/docker_mock.py').read_text(encoding='utf-8'), encoding='utf-8')
        if os.name == 'nt':
            (self.bin / 'docker.cmd').write_text(f'@"{sys.executable}" "{mock}" %*\n')
            self.command = ['cmd.exe', '/d', '/c', str(self.root / 'INSTALL-WINDOWS.cmd')]
        else:
            shim = self.bin / 'docker'
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{mock}" "$@"\n')
            shim.chmod(0o755)
            self.command = ['/bin/bash', str(self.root / 'INSTALL-MACOS.command' if sys.platform == 'darwin' else self.root / 'INSTALL-LINUX.sh')]
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
                    'INSTALLER_TEST_BUNDLE': str(self.root), 'INSTALLER_TEST_REPO': str(REPO),
                    'SPOOLDESK_INSTALLER_NO_PAUSE': '1', 'SPOOLDESK_INSTALLER_NO_BROWSER': '1'}
        self.env.pop('DOCKER_HOST', None)
        self.env.pop('PSModulePath', None)  # Match the Windows .cmd launcher boundary.
        self.env.pop('PSMODULEPATH', None)

    def run_launcher(self, input=None, **extra):
        return subprocess.run(self.command, cwd=self.root, env={**self.env, **extra},
                              input=input, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)

    def test_fresh_resume_and_host_overrides(self):
        result = self.run_launcher(POSTGRES_PASSWORD='unrelated-shell-value', SPOOLDESK_IMAGE='other:image')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        original = (self.root / 'spooldesk/.env').read_bytes()
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(original, (self.root / 'spooldesk/.env').read_bytes())
        calls = [json.loads(line) for line in (self.root / 'mock-calls.jsonl').read_text().splitlines()]
        run = next(args for args in calls if args[0] == 'run')
        self.assertIn('none', run)
        self.assertIn('--read-only', run)
        self.assertFalse(any('docker.sock' in arg for arg in run))
        self.assertTrue(any('--no-build' in args and '--pull' in args for args in calls))
        self.assertFalse(any('down' in args for args in calls))
        self.assertTrue(all(args == ['rm', 'installer-test-lock'] for args in calls if args[0] == 'rm'))
        self.assertFalse((self.root / 'mock-lock').exists())

    def test_concurrent_installer_refused_without_removing_its_lock(self):
        result = self.run_launcher(INSTALLER_TEST_LOCKED='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'spooldesk').exists())
        calls = [json.loads(line) for line in (self.root / 'mock-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any(args[0] in ('rm', 'run', 'pull') for args in calls))

    def test_containerd_manifest_id_is_accepted(self):
        result = self.run_launcher(INSTALLER_TEST_IMAGE_STORE='containerd')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        original = (self.root / 'spooldesk/.env').read_bytes()
        result = self.run_launcher(INSTALLER_TEST_IMAGE_STORE='containerd')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'spooldesk/.env').read_bytes(), original)

    def test_unknown_image_and_wrong_platform_fail_before_execution(self):
        for extra in ({'INSTALLER_TEST_IMAGE_STORE': 'unknown'}, {'INSTALLER_TEST_IMAGE_PLATFORM': 'linux/arm64'}):
            result = self.run_launcher(**extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Loaded image differs', result.stdout + result.stderr)
            self.assertFalse((self.root / 'spooldesk').exists())
        calls = [json.loads(line) for line in (self.root / 'mock-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any(args[0] in ('create', 'run', 'pull', 'rm') for args in calls))

    def test_tampered_accepted_ids_fail_checksum_before_load(self):
        (self.root / 'package/IMAGE_IDS').write_text('sha256:' + 'd' * 64 + '\n')
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stdout + result.stderr)
        self.assertFalse((self.root / 'mock-calls.jsonl').exists())

    def test_tamper_and_existing_foreign_storage_stop_before_start(self):
        result = self.run_launcher(INSTALLER_TEST_FOREIGN='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'spooldesk').exists())
        (self.root / 'installer/configure.py').write_text('tampered')
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stdout + result.stderr)

    def test_failed_pull_can_resume_without_reset(self):
        first = self.run_launcher(INSTALLER_TEST_FAIL_PULL='1')
        self.assertNotEqual(first.returncode, 0)
        original = (self.root / 'spooldesk/.env').read_bytes()
        second = self.run_launcher()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(original, (self.root / 'spooldesk/.env').read_bytes())

    def test_actual_russian_wizard_reprompts_and_preserves_hidden_password(self):
        answers = ['bad language', '2', '80', '8011', 'bad port', '8123',
                   'short', 'unsupported$character', PASSWORD, 'different-password',
                   PASSWORD, PASSWORD, 'unknown answer', '1']
        result = self.run_launcher(input='\n'.join(answers) + '\n', INSTALLER_TEST_INTERACTIVE='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout + result.stderr
        for key in ('password_length', 'password_chars', 'password_mismatch', 'bad_choice'):
            self.assertIn(configure.TEXT[key][1], output)
        self.assertNotIn(PASSWORD, output)
        state = json.loads((self.root / 'spooldesk/INSTALLATION.json').read_text())
        self.assertEqual((state['port'], state['language']), (8123, 1))

    def test_explicit_cancel_and_eof_are_not_docker_errors(self):
        for input_text in (f'2\n8123\n{PASSWORD}\n{PASSWORD}\n0\n', ''):
            with self.subTest(input=input_text == ''):
                result = self.run_launcher(input=input_text, INSTALLER_TEST_INTERACTIVE='1')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('Installation stopped', result.stdout + result.stderr)
                self.assertFalse((self.root / 'spooldesk').exists())
                self.assertFalse((self.root / 'mock-lock').exists())
        calls = [json.loads(line) for line in (self.root / 'mock-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any(a[0] == 'pull' or 'up' in a for a in calls))

    def test_docker_and_compose_unavailable_do_not_create_settings(self):
        for extra in ({'INSTALLER_TEST_DOCKER_DOWN': '1'}, {'INSTALLER_TEST_NO_COMPOSE': '1'}):
            result = self.run_launcher(**extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'spooldesk').exists())
            self.assertFalse((self.root / 'mock-lock').exists())

    def test_occupied_port_stops_before_services_and_can_resume(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            identity = configure.image_identity(self.root, 'amd64')
            configure.create_installation(self.root, identity, port, PASSWORD, 0)
            original = (self.root / 'spooldesk/.env').read_bytes()
            first = self.run_launcher()
            self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn(f'Port {port} is unavailable', first.stdout + first.stderr)
            self.assertFalse((self.root / 'mock-active').exists())
        second = self.run_launcher()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(original, (self.root / 'spooldesk/.env').read_bytes())

    def test_unhealthy_dashboard_exits_without_waiting_full_timeout(self):
        result = self.run_launcher(INSTALLER_TEST_HEALTH='unhealthy')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not become healthy', result.stdout + result.stderr)
        self.assertTrue((self.root / 'spooldesk/.env').exists())
        self.assertFalse((self.root / 'mock-lock').exists())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux PTY coverage; Windows has native cmd input coverage')
    def test_terminal_dialog_masks_password_and_accepts_russian_confirmation(self):
        import pty
        import select
        master, slave = pty.openpty()
        process = subprocess.Popen(self.command, cwd=self.root,
                                   env={**self.env, 'INSTALLER_TEST_INTERACTIVE': '1'},
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 45

        def receive_until(marker):
            while marker.encode('utf-8') not in output:
                if time.monotonic() > deadline:
                    self.fail('Terminal dialog timed out: ' + output.decode('utf-8', errors='replace'))
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        self.fail('Terminal closed before expected prompt')

        try:
            for marker, answer in [('Language /', '2'), ('Порт кабинета', '8123'),
                                   ('Пароль владельца', PASSWORD), ('Повтори пароль', PASSWORD),
                                   ('1 = установить', '?'), ('Ответ не распознан', 'д')]:
                receive_until(marker)
                os.write(master, (answer + '\n').encode('utf-8'))
            receive_until('Ready:')
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertNotIn(PASSWORD, output.decode('utf-8'))
            self.assertTrue((self.root / 'spooldesk/.env').exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
