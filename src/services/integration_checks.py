"""Explicit credential diagnostics. Never send, fetch, mark or replay messages.

Only stable result codes leave this module. Provider exception text and bodies
may contain credentials or customer data and must not reach storage or the UI.
"""
import asyncio
import imaplib
import smtplib
import ssl

import aiohttp
import httpx

TIMEOUT = 8
SLACK_REQUIRED = ('chat:write', 'im:history', 'users:read')
SLACK_OPTIONAL = ('im:write', 'files:read', 'files:write', 'channels:history', 'groups:history')


def result(part: str, status: str, code: str, *, missing=()) -> dict:
    return {'part': part, 'status': status, 'code': code, 'missing': list(missing)}


async def _slack(values):
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        response = await client.post('https://slack.com/api/auth.test',
            headers={'Authorization': 'Bearer ' + values['bot_token']})
        if response.status_code == 429:
            return [result('slack', 'warning', 'rate_limited')]
        if response.status_code != 200:
            return [result('slack', 'error', 'provider_unavailable')]
        body = response.json()
        if not isinstance(body, dict) or body.get('ok') is not True:
            return [result('slack', 'error', 'authentication_failed')]
        scopes = {v.strip() for v in response.headers.get('x-oauth-scopes', '').split(',') if v.strip()}
        if not scopes:
            return [result('slack', 'warning', 'slack_scopes_unknown')]
        required = [s for s in SLACK_REQUIRED if s not in scopes]
        optional = [s for s in SLACK_OPTIONAL if s not in scopes]
        if required:
            return [result('slack', 'error', 'slack_scopes_missing', missing=required)]
        return [result('slack', 'warning' if optional else 'ok',
            'slack_optional_scopes' if optional else 'authentication_ok', missing=optional)]


async def _telegram(values):
    # aiohttp does not log the token-bearing request URL on successful requests.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as client:
        base = 'https://api.telegram.org/bot' + values['bot_token']
        for method in ('getMe', 'getWebhookInfo'):
            async with client.get(base + '/' + method, allow_redirects=False) as response:
                if response.status == 429:
                    return [result('telegram', 'warning', 'rate_limited')]
                if response.status != 200:
                    return [result('telegram', 'error', 'authentication_failed')]
                data = await response.json()
                if not isinstance(data, dict) or data.get('ok') is not True:
                    return [result('telegram', 'error', 'authentication_failed')]
                if method == 'getWebhookInfo' and data.get('result', {}).get('url'):
                    return [result('telegram', 'error', 'telegram_webhook_active')]
    return [result('telegram', 'ok', 'authentication_ok')]


def _mail_part(part, values):
    login = values.get('login') or values['address']
    try:
        if part == 'imap':
            box = imaplib.IMAP4_SSL(values['imap_host'], int(values['imap_port']),
                timeout=TIMEOUT, ssl_context=ssl.create_default_context())
            try:
                box.login(login, values['password'])
                status, _ = box.select('INBOX', readonly=True)
                if status != 'OK':
                    return result(part, 'error', 'imap_inbox_unavailable')
            finally:
                try:
                    box.logout()
                except Exception:
                    pass
        else:
            port = int(values['smtp_port'])
            if port == 465:
                connection = smtplib.SMTP_SSL(values['smtp_host'], port, timeout=TIMEOUT,
                    context=ssl.create_default_context())
            else:
                connection = smtplib.SMTP(values['smtp_host'], port, timeout=TIMEOUT)
            with connection as smtp:
                smtp.ehlo()
                if port != 465:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                smtp.login(login, values['password'])
        return result(part, 'ok', 'authentication_ok')
    except ssl.SSLError:
        return result(part, 'error', 'tls_failed')
    except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
        return result(part, 'error', 'authentication_failed')
    except smtplib.SMTPNotSupportedError:
        return result(part, 'error', 'mail_tls_or_auth_unavailable')
    except Exception:
        return result(part, 'error', 'connection_failed')


async def probe(channel: str, values: dict) -> list[dict]:
    try:
        async with asyncio.timeout(35):
            if channel == 'slack':
                return await _slack(values)
            if channel == 'telegram':
                return await _telegram(values)
            if channel == 'email':
                return list(await asyncio.gather(*(
                    asyncio.to_thread(_mail_part, part, values) for part in ('imap', 'smtp'))))
    except (TimeoutError, httpx.TimeoutException):
        return [result(channel, 'error', 'connection_timeout')]
    except Exception:
        return [result(channel, 'error', 'connection_failed')]
    return [result(channel, 'error', 'configuration_incomplete')]
