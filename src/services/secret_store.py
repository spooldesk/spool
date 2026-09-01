"""Values that go in the database but must not be readable out of a backup.

There is exactly one kind of these today: the API key for whichever LLM
provider an instance is using. It lives in the database rather than in `.env`
so that a managed customer can change providers from their own dashboard
instead of asking somebody with SSH — which was the whole point of making the
provider selectable.

**What this protects, precisely.** `make backup` archives the database and the
attachments. It does not archive `.env`, and it does not archive the key file
described below. So an archive that leaves the machine — to S3, to a storage
box, to somebody's laptop — carries the API key as ciphertext and nothing
that opens it. That is the threat this exists for, and it is a real one: the
archive is the copy that travels.

**What it does not protect against.** Anybody who can read the server can
read both halves. This is not a defence against server compromise and is not
sold as one. Encryption at rest with the key on the same host never is.

**The restore footgun, and how it fails.** Restore that archive onto a fresh
machine without carrying the key across and the ciphertext is undecryptable.
`decrypt()` returns None in that case rather than raising, callers treat it
as "no key configured", and the dashboard says so plainly — because an
instance that cannot read its own API key should degrade to "AI is off and
here is why", never to a stack trace on the tickets page.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

#: Set this in .env and the key never touches the data directory at all.
#: scripts/setup.py writes one for new installs.
ENV_VAR = "SECRETS_KEY"

#: Where a key is kept when the environment does not carry one. Deliberately
#: beside the database rather than in it — a key stored in the thing it
#: encrypts is decoration — and deliberately not in the backup archive.
KEY_FILENAME = "secrets.key"

_cached: Fernet | None = None


def _data_dir() -> Path:
    """The directory the database lives in.

    Derived from DATABASE_URL so the key follows the data: a deployment that
    moved its volume did not mean to leave the key behind.
    """
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("sqlite"):
        path = url.split("///")[-1].split("?")[0]
        if path:
            parent = Path(path).parent
            if parent and str(parent) not in (".", ""):
                return parent
    return Path("data") if Path("data").exists() else Path(".")


def _material() -> str:
    """The secret this derives a cipher from, generating one if need be."""
    from_env = os.getenv(ENV_VAR, "").strip()
    if from_env:
        return from_env

    path = _data_dir() / KEY_FILENAME
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    # No key anywhere. Make one rather than refusing: an operator who has
    # just been asked for an API key should not first have to go and edit a
    # file on the server, which is the friction this feature exists to remove.
    material = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(material + "\n", encoding="utf-8")
        # Owner-only. Best effort: on Windows this is close to meaningless,
        # and failing to tighten a file is not a reason to refuse to work.
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover
            pass
        logger.info(
            "Generated a secrets key at %s. Back this file up separately from "
            "the archive — restoring the database without it leaves stored API "
            "keys unreadable.", path,
        )
    except OSError:  # pragma: no cover
        # A read-only data directory. The cipher still works for this process,
        # so an operator can use the dashboard now; it just will not survive a
        # restart, and they are told.
        logger.warning(
            "Could not write %s. API keys stored now will be unreadable after "
            "a restart — set %s in the environment instead.", path, ENV_VAR,
        )
    return material


def _cipher() -> Fernet:
    global _cached
    if _cached is None:
        # Fernet wants 32 url-safe base64 bytes; the material is any string a
        # person or a generator produced, so hash it into shape.
        digest = hashlib.sha256(_material().encode("utf-8")).digest()
        _cached = Fernet(base64.urlsafe_b64encode(digest))
    return _cached


def reset_cache() -> None:
    """Forget the derived cipher. For tests, and for a changed environment."""
    global _cached
    _cached = None


def encrypt(plaintext: str) -> str:
    """Ciphertext for storing. An empty value stays empty rather than
    becoming an encrypted empty string, so "unset" survives a round trip."""
    if not plaintext:
        return ""
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str | None:
    """The plaintext, or None if this instance cannot read it.

    None is a real answer and callers must handle it: a database restored
    without its key, or a key that was rotated, both land here. Returning None
    rather than raising is what keeps that a disabled feature with an
    explanation instead of an exception on an unrelated page.
    """
    if not ciphertext:
        return None
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        logger.warning(
            "A stored secret could not be decrypted. The database is probably "
            "a restore taken without %s or %s; re-enter the value to fix it.",
            ENV_VAR, KEY_FILENAME,
        )
        return None


def looks_encrypted(value: str) -> bool:
    """Whether a stored value has already been through encrypt().

    Fernet tokens begin with a version byte of 0x80, which base64 renders as
    "gAAAAA". Used to leave a value alone rather than double-encrypting it.
    """
    return value.startswith("gAAAAA")


def masked(plaintext: str | None) -> str:
    """What to show instead of a key. Never the key.

    The last four characters are kept because an operator with two accounts
    needs to tell which one is in here, and four is short enough to be
    useless to anyone else.
    """
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "•" * len(plaintext)
    return "•" * 8 + plaintext[-4:]
