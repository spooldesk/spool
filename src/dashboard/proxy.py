"""One place that decides what to believe from a reverse proxy.

There were two. Uvicorn ships `ProxyHeadersMiddleware` enabled by default, and
this application also read `X-Forwarded-For` — and they disagreed, which is
worse than either being wrong alone, because reasoning about the result meant
knowing both. Locally that showed up as the widget's "five new conversations
per address per hour" doing nothing at all: uvicorn had already rewritten
`scope["client"]` from a header the caller chose, and every request looked
like a different visitor.

So uvicorn's is switched off (`--no-proxy-headers` wherever this is started)
and this is the only thing that reads those headers.

Two headers matter, for two different reasons:

`X-Forwarded-For` decides who a request is from, and every rate limit in the
application is keyed on that answer. Getting it wrong does not degrade those
limits, it removes them.

`X-Forwarded-Proto` decides whether we think we are on HTTPS, which sounds
cosmetic and is not: the embed snippet an operator copies, and the base URL
the widget loader talks to, are both built from the request. Behind TLS with
the scheme unset they come out as `http://`, and a browser on an HTTPS page
refuses to load them. The widget silently stops existing.

Both are ignored entirely unless the deployment says how many proxies are in
front, because a header a stranger can send is not evidence of anything.
"""

from __future__ import annotations

import logging

from src.config import load_settings

logger = logging.getLogger(__name__)


class ProxyHeaders:
    """ASGI middleware. Outermost, so everything downstream sees the truth."""

    def __init__(self, app) -> None:
        self.app = app
        # Read once. This is a fact about the deployment, fixed for the life
        # of the process, and re-reading the environment on every request to
        # learn something that cannot change is a cost with nothing behind it.
        self.hops = load_settings().trusted_proxy_hops
        if self.hops:
            logger.info(
                f"Trusting {self.hops} reverse prox{'y' if self.hops == 1 else 'ies'} "
                f"in front; X-Forwarded-For entry {self.hops} from the right "
                f"is the client"
            )

    async def __call__(self, scope, receive, send):
        if self.hops <= 0 or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        forwarded_for: list[str] = []
        proto: str | None = None
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                # A client may send several of these, and a proxy appends its
                # own. Joining them gives the same chain a single combined
                # header would have.
                forwarded_for.extend(
                    part.strip() for part in value.decode("latin1").split(",")
                )
            elif name == b"x-forwarded-proto":
                proto = value.decode("latin1").strip()

        chain = [entry for entry in forwarded_for if entry]
        if len(chain) >= self.hops:
            # Proxies append, so the entry our own proxy wrote is the Nth from
            # the right. Everything to the left of it is whatever the client
            # chose to send, which is exactly as trustworthy as it sounds.
            #
            # Fewer entries than proxies means the request did not come
            # through them: leave the socket address alone rather than
            # promote a forged one.
            scope["client"] = (_host(chain[-self.hops]), 0)

        if proto in ("http", "https", "ws", "wss"):
            scope["scheme"] = (
                proto.replace("http", "ws") if scope["type"] == "websocket" else proto
            )

        return await self.app(scope, receive, send)


def _host(entry: str) -> str:
    """The address out of one X-Forwarded-For entry, without its port.

    Entries are usually a bare address but are allowed to carry a port, and
    IPv6 arrives bracketed. Keeping the port would split one visitor across
    as many buckets as they have source ports, which is the same as having no
    limit.
    """
    entry = entry.strip()
    if entry.startswith("["):                       # [::1]:1234
        host, _, _ = entry[1:].partition("]")
        return host
    if entry.count(":") == 1:                       # 1.2.3.4:1234
        return entry.rsplit(":", 1)[0]
    return entry                                    # bare v4, or bare v6
