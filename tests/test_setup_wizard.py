"""First installation must generate isolated credentials and preserve existing config."""
from dotenv import dotenv_values

from scripts import setup


def test_clean_setup_generates_owner_and_independent_keys(tmp_path, monkeypatch, capsys):
    target = tmp_path / '.env'
    monkeypatch.setattr(setup, 'ENV_PATH', target)
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert setup.main() == 0
    config = dotenv_values(target)
    secrets = [config[k] for k in ('ACCESS_PASSWORD', 'POSTGRES_PASSWORD', 'DASH_SECRET_KEY', 'SECRETS_KEY')]
    assert len(set(secrets)) == 4
    assert all(len(value) >= 16 for value in secrets)
    assert config['POSTGRES_HOST'] == '127.0.0.1'
    assert all(config[k] == '' for k in ('BOT_TOKEN', 'ADMIN_CHAT_ID', 'GEMINI_API_KEY'))
    output = capsys.readouterr().out
    assert 'admin@local' in output and secrets[0] in output
    assert all(value not in output for value in secrets[1:])
    assert 'up -d --build dashboard' in output


def test_setup_declining_overwrite_preserves_exact_credentials(tmp_path, monkeypatch):
    target = tmp_path / '.env'
    original = b'# retain this file\r\nSECRETS_KEY=existing-test-key\r\n'
    target.write_bytes(original)
    monkeypatch.setattr(setup, 'ENV_PATH', target)
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert setup.main() == 0
    assert target.read_bytes() == original
