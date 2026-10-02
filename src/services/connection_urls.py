"""Public callback origins are explicit settings, never inferred from Host."""
import ipaddress
import re
from urllib.parse import urlsplit


def public_origin(value: str) -> str:
    value = value.strip()
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        if (len(value) > 320 or parts.scheme != 'https' or not host
                or parts.username is not None or parts.password is not None
                or parts.path not in ('', '/') or parts.query or parts.fragment
                or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.'))
                    or all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', label) for label in host.split('.'))
                    or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                               for label in host.split('.'))):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
            if address.version == 6:
                host = '[' + host + ']'
        return 'https://' + host + (f':{port}' if port and port != 443 else '')
    except (ValueError, UnicodeError):
        raise ValueError('connection_public_invalid') from None


def usable_origin(value: str) -> str:
    try:
        return public_origin(value)
    except ValueError:
        return ''
