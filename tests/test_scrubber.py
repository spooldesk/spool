"""PII/secret scrubber — the last line of defense before text reaches an LLM."""

from src.ai.scrubber import scrub


def test_bot_token_redacted():
    out = scrub("вот токен 1234567890:AAHdqTcvbXk29fj29fjaksjd82jfka92kfkaq и всё")
    assert "AAHdqTcvbXk" not in out
    assert "[REDACTED]" in out


def test_jwt_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = scrub(f"мой jwt {jwt} спасибо")
    assert jwt not in out
    assert "[REDACTED]" in out


def test_labelled_password_value_redacted_label_kept():
    out = scrub("не могу зайти, password: Hunter2secret")
    assert "Hunter2secret" not in out
    assert "password" in out.lower()


def test_russian_password_label():
    out = scrub("пароль: SuperSecret99 не подходит")
    assert "SuperSecret99" not in out


def test_long_opaque_token_redacted():
    token = "A" * 45
    out = scrub(f"ключ {token} не работает")
    assert token not in out


def test_normal_text_untouched():
    text = "Не работает VPN на сервере barcelona-3, ошибка timeout"
    assert scrub(text) == text


def test_empty_and_none_safe():
    assert scrub("") == ""
