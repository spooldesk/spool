"""Where an attachment lives when no platform is holding it for us.

Every other channel hands back a reference and keeps the bytes: Telegram has
a file_id, Slack has a private URL. The widget has a browser, and a browser
is not storage. So this is the one channel that has to keep its own files,
and `fetch_file` is the seam that makes that nobody else's problem.

On disk rather than in the database, and the reason is what each of the two
is good at. The database is small, hot and backed up often; attachments are
large, immutable and never queried. Putting screenshots in SQLite makes the
file that every request touches grow without limit, and makes the backup of
a working helpdesk a multi-gigabyte copy of things nobody is reading.

The path contains no caller input at all. Not a sanitised filename, not an
extension — a reference this module invented, and nothing else. Path
traversal in an upload endpoint is the oldest bug on the internet, and the
way not to have it is to never build a path out of a stranger's string. The
real filename lives in the database column that already exists for it.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

#: Under the project by default, which in the container is `/app/data` — the
#: volume both services already mount and the database already lives on. A
#: deployment that persists its database therefore persists its attachments,
#: without anyone having to be told about a second volume.
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "attachments"

#: 16 bytes of randomness. This is not a secret — the endpoints check who is
#: asking — but a guessable reference would make them the only check.
_REF_BYTES = 16
_REF_LEN = _REF_BYTES * 2


def root() -> Path:
    """The directory files are kept in.

    Read on each call rather than at import: the tests point it somewhere
    disposable, and a module-level constant would have been fixed before they
    got the chance.
    """
    configured = (os.getenv("ATTACHMENTS_DIR") or "").strip()
    return Path(configured) if configured else _DEFAULT_ROOT


def _valid(ref: str) -> bool:
    """Whether this is a reference we could have issued.

    Checked before the path is built, so a reference that fails this never
    becomes a filesystem operation. `..` and `/` fail it by not being hex.
    """
    ref = (ref or "").strip()
    return len(ref) == _REF_LEN and all(c in "0123456789abcdef" for c in ref)


def _path(ref: str) -> Path:
    # Two levels of fan-out. One directory holding every attachment an
    # instance has ever taken is slow to list and unpleasant to recover from;
    # 256 × 256 keeps any one of them small for as long as this will run.
    return root() / ref[:2] / ref[2:4] / ref


def put(content: bytes) -> str:
    """Store bytes, return the reference that gets them back."""
    ref = secrets.token_hex(_REF_BYTES)
    target = _path(ref)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and moved into place, so a crash mid-write cannot leave a
    # truncated file behind a reference the database already believes in.
    staging = target.with_suffix(".part")
    with staging.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    staging.replace(target)
    return ref


def get(ref: str) -> bytes | None:
    """The bytes behind a reference, or None if it is not ours or not there.

    None rather than an exception for both cases, and deliberately not
    distinguishing them: the caller's honest answer is the same either way,
    and telling a stranger which of their guesses named a real file turns
    this into an oracle.
    """
    if not _valid(ref):
        return None
    try:
        return _path(ref).read_bytes()
    except OSError:
        return None


def delete(ref: str) -> bool:
    """Remove a stored file. True if it was there.

    Used when an operator unsends: the row stays and says a message was
    withdrawn, but the file it carried should stop being fetchable.
    """
    if not _valid(ref):
        return False
    try:
        _path(ref).unlink()
        return True
    except OSError:
        return False


def size(ref: str) -> int | None:
    """How big a stored file is, without reading it into memory."""
    if not _valid(ref):
        return None
    try:
        return _path(ref).stat().st_size
    except OSError:
        return None
