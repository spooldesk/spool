import json
import subprocess
from pathlib import Path

import pytest

from scripts.preflight import compose_environment, evaluate


def valid():
    return {"ACCESS_PASSWORD": "unique-long-bootstrap-value", "DASH_SECRET_KEY": "s" * 48,
            "SECRETS_KEY": "k" * 48, "DASH_COOKIE_SECURE": "true",
            "DATABASE_URL": "postgresql+asyncpg://ci:ci@postgres/support"}


def test_production_can_use_widget_without_telegram():
    assert all(c["ok"] for c in evaluate(valid(), public_url="https://help.example.com"))


def test_incomplete_channels_and_test_mode_fail_without_exposing_secrets():
    config = valid() | {"EMAIL_PASSWORD": "PRIVATE-MAIL-SECRET", "SLACK_BOT_TOKEN": "PRIVATE-SLACK-SECRET", "TESTING": "1"}
    checks = evaluate(config, public_url="https://help.example.com")
    assert {c["name"] for c in checks if not c["ok"]} == {"email", "slack", "test_mode"}
    assert "PRIVATE" not in str(checks)


def test_http_and_insecure_cookies_are_only_allowed_locally():
    config = valid() | {"DASH_COOKIE_SECURE": "false"}
    assert not all(c["ok"] for c in evaluate(config, public_url="http://localhost:8000"))
    assert all(c["ok"] for c in evaluate(config, public_url="http://localhost:8000", production=False))


def test_split_postgres_credentials_and_external_url_are_both_supported():
    split = valid() | {"DATABASE_URL": "", "POSTGRES_HOST": "postgres",
                       "POSTGRES_PASSWORD": "p@ss/word#%"}
    assert all(c["ok"] for c in evaluate(split, public_url="https://help.example.com"))
    assert not next(c for c in evaluate(split | {"POSTGRES_PASSWORD": ""},
                                       public_url="https://help.example.com") if c["name"] == "database")["ok"]
    external = valid() | {"DATABASE_URL": "postgresql+asyncpg://u:p@remote/db"}
    assert all(c["ok"] for c in evaluate(external, public_url="https://help.example.com"))


def test_compose_checks_effective_env_not_the_shell_or_raw_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / "staging.env"
    env_file.write_text("DASH_COOKIE_SECURE=false\n")
    files = [Path("docker-compose.yml"), Path("docker-compose.prod.yml")]
    monkeypatch.setenv("DASH_COOKIE_SECURE", "false")

    def render(command, **kwargs):
        assert command[-3:] == ["config", "--format", "json"]
        assert command.count("-f") == 2
        assert kwargs["env"]["SPOOLDESK_ENV_FILE"] == str(env_file.resolve())
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "services": {"dashboard": {"environment": valid() | {"TRUSTED_PROXY_HOPS": "1"}}}}))

    monkeypatch.setattr(subprocess, "run", render)
    effective = compose_environment(env_file, files)
    assert effective["DASH_COOKIE_SECURE"] == "true"
    assert all(c["ok"] for c in evaluate(effective, public_url="https://help.example.com"))


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, "docker", stderr="PRIVATE-PASSWORD"),
    subprocess.TimeoutExpired("docker", 30, output="PRIVATE-PASSWORD"),
    FileNotFoundError("PRIVATE-PASSWORD"),
])
def test_compose_failures_never_echo_secret_diagnostics(monkeypatch, tmp_path, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError) as exc:
        compose_environment(tmp_path / "staging.env", [Path("docker-compose.yml")])
    assert "PRIVATE" not in str(exc.value)
