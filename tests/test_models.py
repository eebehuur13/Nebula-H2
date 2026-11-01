"""The Bedrock model paths, against stand-in AWS clients: what Nebula sends and how it reads the reply.
They can't show the real service works (Bedrock was blocked on the account), only that Nebula's side is right.
Also: a reranker failure keeps search working and shows up in the search's trace."""

from __future__ import annotations

import io
import json

import pytest

from nebula import models
from nebula.config import settings


class FakeBedrock:
    def __init__(self):
        self.calls: list[dict] = []

    def invoke_model(self, modelId, body, contentType):
        req = json.loads(body)
        self.calls.append({"model": modelId, **req})
        if "texts" in req:  # Cohere: a batch
            out = {"embeddings": {"float": [[float(len(t)), 0.0] for t in req["texts"]]}}
        else:  # Titan: one text
            out = {"embedding": [float(len(req["inputText"])), 1.0]}
        return {"body": io.BytesIO(json.dumps(out).encode())}

    def rerank(self, queries, sources, rerankingConfiguration):
        self.calls.append({"queries": queries, "sources": sources, "config": rerankingConfiguration})
        n = rerankingConfiguration["bedrockRerankingConfiguration"]["numberOfResults"]
        return {"results": [{"index": i, "relevanceScore": 1.0 - i / 10} for i in reversed(range(n))]}


@pytest.fixture
def bedrock(monkeypatch):
    fake = FakeBedrock()
    monkeypatch.setattr(models, "_bedrock", lambda: fake)
    monkeypatch.setattr(models, "_bedrock_agent", lambda: fake)
    return fake


def test_cohere_embeddings_go_in_batches_of_96(bedrock, monkeypatch):
    monkeypatch.setattr(settings(), "embed_provider", "bedrock-cohere")
    texts = [f"passage {i}" for i in range(200)]
    vecs = models._embed_sync(texts, "document")
    assert len(vecs) == 200 and vecs[0] == [float(len("passage 0")), 0.0]
    assert [len(c["texts"]) for c in bedrock.calls] == [96, 96, 8]
    assert all(c["model"] == "us.cohere.embed-v4:0" and c["input_type"] == "search_document" for c in bedrock.calls)
    assert bedrock.calls[0]["output_dimension"] == settings().embed_dim
    models._embed_sync(["a question"], "query")
    assert bedrock.calls[-1]["input_type"] == "search_query"
    assert models.embed_model_name() == "bedrock:cohere.embed-v4:1024"


def test_titan_embeds_each_text_and_keeps_the_order(bedrock, monkeypatch):
    monkeypatch.setattr(settings(), "embed_provider", "bedrock-titan")
    texts = ["a" * n for n in range(1, 30)]
    vecs = models._embed_sync(texts, "document")
    assert [v[0] for v in vecs] == [float(n) for n in range(1, 30)]  # in order, although sent in parallel
    assert len(bedrock.calls) == 29 and all(c["normalize"] is True for c in bedrock.calls)


def test_cohere_rerank_request_and_order(bedrock, monkeypatch):
    monkeypatch.setattr(settings(), "rerank_provider", "bedrock-cohere")
    order = models._rerank_sync("which district?", ["one", "two", "three"], top_n=3)
    assert order == [(2, 0.8), (1, 0.9), (0, 1.0)]
    call = bedrock.calls[0]
    assert call["queries"][0]["textQuery"]["text"] == "which district?"
    assert len(call["sources"]) == 3
    assert call["config"]["bedrockRerankingConfiguration"]["modelConfiguration"]["modelArn"].endswith(
        "cohere.rerank-v3-5:0")


async def test_a_reranker_failure_is_recorded_and_search_still_answers(mcp, monkeypatch):
    from helpers import MEMBER, call as tool_call, drain, drive_file
    from nebula import db

    await drive_file("Rerank outage note", "Kurnool attendance dipped in July.", emails=[MEMBER])
    await drain()

    async def broken(query, docs, top_n):
        raise RuntimeError("reranker is down")

    monkeypatch.setattr(models, "rerank", broken)
    r = await tool_call(mcp, MEMBER, "search", query="Kurnool attendance")
    assert any(x["title"] == "Rerank outage note" for x in r["results"])
    row = await db.fetch_one("select trace from tool_calls where tool='search' order by id desc limit 1")
    assert "reranker is down" in row["trace"]["rerank_failed"]
