"""Process-local snapshot of owner-managed connection settings.

Populated from encrypted DB records by the runtime refresher. Environment is
only the fallback for channels that have never been saved in the dashboard.
"""
import os

_values: dict[str, str] = {}


def get(name: str, default: str = "") -> str:
    return _values.get(name, os.getenv(name, default))


def install(values: dict[str, str]) -> None:
    global _values
    _values = dict(values)


def reset() -> None:
    install({})
