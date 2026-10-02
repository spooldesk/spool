"""Tests for src/ai/knowledge.py -- grounded, operator-reviewed AI drafts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import knowledge


@pytest.fixture(autouse=True)
def source_policy(monkeypatch):
    monkeypatch.setattr(knowledge.policy, 'permitted_sources', AsyncMock(side_effect=lambda rows: rows))


def _article(id_, title="Reset your password", slug="reset-password",
             body="Go to settings and click reset.", category="billing",
             is_published=True):
    return SimpleNamespace(
        id=id_, title=title, slug=slug, body=body,
        category=category, is_published=is_published,
    )


def _fake_db_session(ticket):
    @asynccontextmanager
    async def _session():
        yield SimpleNamespace(
            get=AsyncMock(return_value=ticket),
            add=lambda obj: setattr(obj, "id", 999),
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
    return _session


def _fake_client(text, model="test-model"):
    response = SimpleNamespace(text=text, model=model, tokens_in=42, tokens_out=17)
    return SimpleNamespace(complete=AsyncMock(return_value=response))


# ---------------------------------------------------------------- parsing


def test_parse_response_valid():
    parsed = knowledge._parse_response(
        json.dumps({"answer": "Click reset.", "source_ids": [1, 2]})
    )
    assert parsed == ("Click reset.", [1, 2])


@pytest.mark.parametrize("text", [
    "not json",
    json.dumps(["not", "a", "dict"]),
    json.dumps({"source_ids": [1]}),
    json.dumps({"answer": 5, "source_ids": [1]}),
    json.dumps({"answer": "ok", "source_ids": "1"}),
    json.dumps({"answer": "ok", "source_ids": [1, "2"]}),
])
def test_parse_response_malformed(text):
    assert knowledge._parse_response(text) is None


# ---------------------------------------------------------------- policy gate


@pytest.mark.asyncio
async def test_disabled_policy_skips_provider_call(monkeypatch):
    monkeypatch.setattr(knowledge.policy, "ticket_allowed", AsyncMock(return_value=False))
    get_client_mock = AsyncMock()
    monkeypatch.setattr(knowledge, "get_client", get_client_mock)

    result = await knowledge.suggest_from_knowledge(1, "How do I reset my password?")

    assert result.suggested_text is None
    assert result.ai_call_id is None
    assert result.sources == []
    get_client_mock.assert_not_called()


# ---------------------------------------------------------------- bounded context


def test_user_prompt_bounds_articles_and_length():
    articles = [_article(i, body="x" * 5000) for i in range(1, 7)]
    prompt = knowledge._user_prompt("question", articles)

    included_ids = [f"[id={i}]" for i in range(1, 7)]
    present = [marker for marker in included_ids if marker in prompt]
    assert len(present) == knowledge.MAX_ARTICLES

    for line in prompt.split("\n\n")[2:]:
        body_part = line.split("\n", 1)[1] if "\n" in line else ""
        assert len(body_part) <= knowledge.MAX_ARTICLE_CHARS


@pytest.mark.asyncio
async def test_question_truncated_before_candidate_search(monkeypatch):
    ticket = SimpleNamespace(id=1, category="billing")
    monkeypatch.setattr("src.database.db.db_session", _fake_db_session(ticket))
    monkeypatch.setattr(knowledge.policy, "ticket_allowed", AsyncMock(return_value=True))

    candidates_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(knowledge.article_service, "candidates_for", candidates_mock)

    long_text = "a" * (knowledge.MAX_QUESTION_CHARS + 500)
    await knowledge.suggest_from_knowledge(1, long_text)

    called_question = candidates_mock.call_args.args[0]
    assert len(called_question) <= knowledge.MAX_QUESTION_CHARS


# ---------------------------------------------------------------- publication recheck


@pytest.mark.asyncio
async def test_publication_recheck_withholds_draft_if_any_citation_revoked(monkeypatch):
    ticket = SimpleNamespace(id=1, category="billing")
    monkeypatch.setattr("src.database.db.db_session", _fake_db_session(ticket))
    monkeypatch.setattr(knowledge.policy, "ticket_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        knowledge.policy, "permitted_sources", AsyncMock(side_effect=lambda rows: rows)
    )

    published_article = _article(10, title="Reset password", slug="reset-password")
    revoked_article = _article(20, title="Old draft", is_published=False)
    monkeypatch.setattr(
        knowledge.article_service, "candidates_for",
        AsyncMock(return_value=[published_article, revoked_article]),
    )
    monkeypatch.setattr(
        knowledge.article_service, "by_id",
        AsyncMock(side_effect=lambda aid: published_article if aid == 10 else revoked_article),
    )

    response_text = json.dumps({"answer": "Reset it from settings.", "source_ids": [10, 20]})
    monkeypatch.setattr(
        knowledge, "get_client", AsyncMock(return_value=_fake_client(response_text))
    )

    result = await knowledge.suggest_from_knowledge(1, "How do I reset my password?")

    assert result.suggested_text is None
    assert result.sources == []


@pytest.mark.asyncio
async def test_all_sources_revoked_withholds_draft(monkeypatch):
    ticket = SimpleNamespace(id=1, category="billing")
    monkeypatch.setattr("src.database.db.db_session", _fake_db_session(ticket))
    monkeypatch.setattr(knowledge.policy, "ticket_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(knowledge.policy, "permitted_sources", AsyncMock(return_value=[]))

    article = _article(10)
    monkeypatch.setattr(
        knowledge.article_service, "candidates_for", AsyncMock(return_value=[article])
    )
    monkeypatch.setattr(knowledge.article_service, "by_id", AsyncMock(return_value=article))

    response_text = json.dumps({"answer": "Reset it from settings.", "source_ids": [10]})
    monkeypatch.setattr(
        knowledge, "get_client", AsyncMock(return_value=_fake_client(response_text))
    )

    result = await knowledge.suggest_from_knowledge(1, "How do I reset my password?")

    assert result.suggested_text is None
    assert result.ai_call_id is None
    assert result.sources == []


@pytest.mark.asyncio
async def test_unknown_source_id_yields_no_draft(monkeypatch):
    ticket = SimpleNamespace(id=1, category="billing")
    monkeypatch.setattr("src.database.db.db_session", _fake_db_session(ticket))
    monkeypatch.setattr(knowledge.policy, "ticket_allowed", AsyncMock(return_value=True))

    article = _article(10)
    monkeypatch.setattr(
        knowledge.article_service, "candidates_for", AsyncMock(return_value=[article])
    )

    response_text = json.dumps({"answer": "Reset it.", "source_ids": [999]})
    monkeypatch.setattr(
        knowledge, "get_client", AsyncMock(return_value=_fake_client(response_text))
    )

    result = await knowledge.suggest_from_knowledge(1, "How do I reset my password?")

    assert result.suggested_text is None


@pytest.mark.asyncio
async def test_unavailable_model_yields_no_draft(monkeypatch):
    ticket = SimpleNamespace(id=1, category="billing")
    monkeypatch.setattr("src.database.db.db_session", _fake_db_session(ticket))
    monkeypatch.setattr(knowledge.policy, "ticket_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        knowledge.article_service, "candidates_for", AsyncMock(return_value=[_article(10)])
    )
    monkeypatch.setattr(knowledge, "get_client", AsyncMock(return_value=None))

    result = await knowledge.suggest_from_knowledge(1, "How do I reset my password?")

    assert result.suggested_text is None


@pytest.mark.parametrize('outcome',['success','revoked','changed','policy_revoked','mixed_unknown'])
async def test_citations_are_rechecked_after_completion(monkeypatch,outcome):
    ticket=SimpleNamespace(id=1,category='billing')
    monkeypatch.setattr('src.database.db.db_session',_fake_db_session(ticket))
    decisions=[True,True,False] if outcome=='policy_revoked' else [True,True,True]
    monkeypatch.setattr(knowledge.policy,'ticket_allowed',AsyncMock(side_effect=decisions))
    article=_article(10)
    current=_article(10,is_published=outcome!='revoked',body='Changed rules' if outcome=='changed' else article.body)
    monkeypatch.setattr(knowledge.article_service,'candidates_for',AsyncMock(return_value=[article]))
    monkeypatch.setattr(knowledge.article_service,'by_id',AsyncMock(return_value=current))
    client=_fake_client(json.dumps({'answer':'Reset in settings.','source_ids':[10,999] if outcome=='mixed_unknown' else [10]}))
    monkeypatch.setattr(knowledge,'get_client',AsyncMock(return_value=client))
    result=await knowledge.suggest_from_knowledge(1,'How can I reset my password?')
    client.complete.assert_awaited_once()
    if outcome=='success':
        assert result.suggested_text=='Reset in settings.' and result.ai_call_id==999
        assert result.sources==[{'id':10,'title':article.title,'url':'/help/reset-password'}]
    else:
        assert result.suggested_text is None and not result.sources


async def test_disallowed_article_never_reaches_provider(monkeypatch):
    monkeypatch.setattr('src.database.db.db_session',_fake_db_session(SimpleNamespace(id=1,category='billing')))
    monkeypatch.setattr(knowledge.policy,'ticket_allowed',AsyncMock(return_value=True))
    monkeypatch.setattr(knowledge.article_service,'candidates_for',AsyncMock(return_value=[_article(10)]))
    monkeypatch.setattr(knowledge.policy,'permitted_sources',AsyncMock(return_value=[]))
    provider=AsyncMock();monkeypatch.setattr(knowledge,'get_client',provider)
    assert (await knowledge.suggest_from_knowledge(1,'Reset')).suggested_text is None
    provider.assert_not_called()
