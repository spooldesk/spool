"""Distribute the same identity tokens to the static site and the application."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(*, check: bool = False) -> None:
    source = (ROOT / 'assets/brand/tokens.css').read_bytes()
    for relative in ('docs/brand.css', 'src/dashboard/static/brand.css'):
        target = ROOT / relative
        if check:
            if not target.exists() or target.read_bytes() != source:
                raise ValueError(f'Brand tokens are stale: {relative}')
        else:
            target.write_bytes(source)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    build(check=parser.parse_args().check)
