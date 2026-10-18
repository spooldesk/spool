"""No human-language string literals outside the translation catalogs.

The dashboard shipped as "fully English" while ~60 Russian literals were still
hardcoded in route handlers, service validation and duration formatters. They
survived review because they only appear on error paths, which the page-level
leak guard never walks.

This test reads the AST rather than the raw text, so Russian *comments* and
docstrings stay allowed — they explain the code to its maintainer and never
reach a user.
"""

import ast
import io
import pathlib
import re

import pytest

CYRILLIC = re.compile(r"[Ѐ-ӿ]")
REPO = pathlib.Path(__file__).resolve().parents[1]

# Modules that are allowed to contain Russian strings, and why.
ALLOWED = {
    # The catalogs themselves.
    "src/dashboard/i18n.py",
    "src/dashboard/workspace_i18n.py",
    "src/dashboard/connection_i18n.py",
    "src/dashboard/assistant_i18n.py",
    "src/dashboard/customer_ai_i18n.py",
    "src/utils/i18n.py",
    # Legacy sentinels kept only so old databases keep rendering. They are
    # matched, never displayed — see LEGACY_CUSTOM_VALUES / LEGACY_AI_VALUES.
    "src/services/close_reason_service.py",
    # One-time data migrations that rewrite the old literals.
    "src/database/db.py",
    # Cyrillic as DATA, not as interface. These match what people write, so
    # translating them would break the matching:
    #   the scrubber redacts "пароль: …" as well as "password: …",
    "src/ai/scrubber.py",
    #   the history search drops bare acknowledgements ("проверяю", "гляну")
    #   so the model sees resolutions instead of noise,
    "src/ai/suggest_from_history.py",
    #   and the prompt teaches the model what an acknowledgement looks like in
    #   the languages users actually write in.
    "src/ai/prompts.py",
    #   The mail parser recognises the line a client puts above a quoted
    #   reply — "…написал:" as well as "…wrote:". These are what other
    #   people's software emits, so translating them would stop the quoted
    #   history being trimmed and put four screens of it in every ticket.
    "src/channels/email_parse.py",
    #   The customer agent hands a conversation to a person when the customer
    #   asks for one — "хочу оператора", "позовите человека" as well as "I
    #   want a human". Translating that pattern would stop it recognising the
    #   request in the languages customers actually write in.
    "src/ai/customer_agent.py",
}

# Where user-visible text actually gets produced.
#
# The handlers have two audiences and two catalogs: text the customer reads
# goes through get_text() in src/utils/i18n.py and follows THEIR language;
# text posted into the admin chat goes through _op() and follows the instance
# UI language. Neither may be a literal.
# src/config and src/ai were outside this list, and that is how a leftover
# module shipped the category names of the one help desk this started as —
# in Russian, naming that team's own tools — as the product's defaults. Scan
# all of src/ instead of naming the directories that happen to matter today.
SCANNED = ["src"]


def _iter_python_files():
    for root in SCANNED:
        for path in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWED:
                continue
            yield rel, path


def _cyrillic_literals(path: pathlib.Path):
    """Every Cyrillic string literal in the file, excluding docstrings."""
    tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=str(path))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if CYRILLIC.search(node.value) and node.value not in docstrings:
                found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            literal = "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if CYRILLIC.search(literal):
                found.append((node.lineno, f"f-string: {literal}"))
    return found


@pytest.mark.parametrize("rel,path", list(_iter_python_files()), ids=lambda v: v if isinstance(v, str) else "")
def test_no_cyrillic_string_literals(rel, path):
    hits = _cyrillic_literals(path)
    assert not hits, (
        f"{rel} has hardcoded Russian strings — move them into "
        f"src/dashboard/i18n.py and call t():\n" +
        "\n".join(f"  line {ln}: {text[:80]!r}" for ln, text in hits)
    )


def test_the_guard_can_actually_see_a_leak(tmp_path):
    """A guard that never fires is worse than no guard."""
    sample = tmp_path / "leaky.py"
    sample.write_text(
        '"""Русский докстринг — разрешён."""\n'
        "# Русский комментарий — тоже разрешён\n"
        'MESSAGE = "Тикет не найден"\n'
        'OK = "Ticket not found"\n',
        encoding="utf-8",
    )
    hits = _cyrillic_literals(sample)
    assert [text for _, text in hits] == ["Тикет не найден"]
