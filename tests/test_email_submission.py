"""SMTP submission uses complete mail headers and never real mail servers."""
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

import pytest

from src.channels.email import EmailChannel


@pytest.mark.parametrize('port', [465, 587])
@pytest.mark.parametrize('attachment', [False, True])
async def test_smtp_submits_dated_reply_with_correct_recipient(monkeypatch, port, attachment):
    for key, value in {
        'EMAIL_ADDRESS': 'support@example.test', 'EMAIL_LOGIN': 'mailbox@example.test',
        'EMAIL_PASSWORD': 'test-only', 'EMAIL_IMAP_HOST': 'imap.example.test',
        'EMAIL_SMTP_HOST': 'smtp.example.test', 'EMAIL_SMTP_PORT': str(port),
        'EMAIL_FROM_NAME': 'Support',
    }.items():
        monkeypatch.setenv(key, value)
    submitted = []
    tls = port == 465

    class Server:
        def __init__(self, host, actual_port, **kwargs):
            assert host == 'smtp.example.test' and actual_port == port
            assert kwargs['timeout'] > 0
            if port == 465:
                assert kwargs['context'].check_hostname

        def __enter__(self): return self
        def __exit__(self, *args): return False
        def ehlo(self): pass

        def starttls(self, *, context):
            nonlocal tls
            assert context.check_hostname
            tls = True

        def login(self, username, password):
            assert tls and username == 'mailbox@example.test' and password == 'test-only'

        def send_message(self, message):
            # Inspect serialized MIME, the same boundary used by smtplib.
            wire = message.as_bytes(policy=policy.SMTP)
            submitted.append(BytesParser(policy=policy.default).parsebytes(wire))
            return {}

    monkeypatch.setattr('src.channels.email.smtplib.SMTP_SSL', Server)
    monkeypatch.setattr('src.channels.email.smtplib.SMTP', Server)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    channel = EmailChannel()
    result = await channel._send(
        'customer@example.test', 'Ответ клиенту', subject='Re: Question',
        message_id='<reply@example.test>', in_reply_to='<question@example.test>',
        attachment=('proof.txt', b'proof') if attachment else None,
    )
    assert result.ok and len(submitted) == 1
    message = submitted[0]
    assert len(message.get_all('Date', [])) == 1
    sent_at = parsedate_to_datetime(message['Date'])
    assert sent_at.tzinfo is not None
    assert before <= sent_at <= datetime.now(timezone.utc)
    assert message['To'] == 'customer@example.test'
    assert message['From'].addresses[0].addr_spec == 'support@example.test'
    assert message['In-Reply-To'] == message['References'] == '<question@example.test>'
    assert message['Message-ID'] == '<reply@example.test>'
    assert message.get_body(preferencelist=('plain',)).get_content().strip() == 'Ответ клиенту'
    files = list(message.iter_attachments())
    assert len(files) == int(attachment)
    if attachment:
        assert files[0].get_payload(decode=True) == b'proof'
