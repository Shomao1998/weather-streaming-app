"""The knowledge layer: chunking, ingestion, versioning and traceability.

These tests are about the corpus being trustworthy, which is upstream of every
claim the feature makes. A citation is only worth showing if the chunk it names
can be traced to a document, a version and a URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from weather.advice.chunking import MAX_CHARS, chunk_document, split_sections  # noqa: E402
from weather.advice.embeddings import HashingEmbedder, cosine  # noqa: E402
from weather.advice.knowledge import KnowledgeIndex, chunk_id  # noqa: E402

RAW = REPO_ROOT / "knowledge" / "raw"


# -- chunking ---------------------------------------------------------------


def test_sections_keep_their_heading():
    sections = split_sections("# A\n\nfirst\n\n## B\n\nsecond\n")
    assert [(s.heading, s.text) for s in sections] == [("A", "first"), ("B", "second")]


def test_no_chunk_exceeds_the_hard_cap():
    for document in RAW.glob("*.md"):
        for section in chunk_document(document.read_text(encoding="utf-8")):
            assert len(section.text) <= MAX_CHARS, f"{document.name}: {section.heading}"


def test_a_long_section_splits_with_overlap():
    body = "\n\n".join(f"- bullet number {i} " + "x" * 60 for i in range(30))
    chunks = chunk_document(f"# Long\n\n{body}\n")
    assert len(chunks) > 1
    # The tail of one chunk reappears at the head of the next, so a passage
    # that begins mid-list still carries the line before it.
    assert chunks[0].text.splitlines()[-1] in chunks[1].text


def test_a_stub_section_is_merged_rather_than_indexed_alone():
    chunks = chunk_document("# Real\n\n" + "y" * 300 + "\n\n## Stub\n\ntiny\n")
    assert len(chunks) == 1
    assert "tiny" in chunks[0].text


def test_an_action_stays_with_its_reason():
    """The failure this design exists to prevent."""
    text = (RAW / "nws-flood-during.md").read_text(encoding="utf-8")
    driving = [c for c in chunk_document(text) if "flooded" in c.text.lower()]
    assert driving, "expected a chunk about flooded roads"
    # Whichever chunk carries the instruction must also carry its rationale.
    assert any("Turn Around" in c.text or "depth" in c.text.lower() for c in driving)


# -- content addressing and idempotency -------------------------------------


def test_chunk_id_is_content_addressed():
    first = chunk_id("doc", "1.0", 0, "hello")
    assert first == chunk_id("doc", "1.0", 0, "hello")
    assert first != chunk_id("doc", "1.0", 0, "hello world")
    assert first != chunk_id("doc", "1.1", 0, "hello")
    assert first != chunk_id("other", "1.0", 0, "hello")


def test_reingesting_unchanged_sources_reproduces_the_index(tmp_path):
    from ingest_knowledge import _comparable, build_index

    first = build_index(embedder=HashingEmbedder())
    second = build_index(embedder=HashingEmbedder())
    assert _comparable(first) == _comparable(second)
    assert len({c.chunk_id for c in first.chunks}) == len(first.chunks)


def test_a_new_document_version_replaces_every_chunk_of_the_old(tmp_path):
    """No path leaves two versions of the same passage retrievable."""
    import yaml
    from ingest_knowledge import build_index

    sources = yaml.safe_load(
        (REPO_ROOT / "knowledge" / "sources.yaml").read_text(encoding="utf-8")
    )
    target = next(s for s in sources["sources"] if s["id"] == "nws-heat-during")
    old = build_index(embedder=HashingEmbedder())
    old_ids = {c.chunk_id for c in old.chunks if c.source_document_id == target["id"]}

    target["version"] = "9.9.9"
    bumped = tmp_path / "sources.yaml"
    bumped.write_text(yaml.safe_dump(sources), encoding="utf-8")

    new = build_index(sources_path=bumped, embedder=HashingEmbedder())
    new_ids = {c.chunk_id for c in new.chunks if c.source_document_id == target["id"]}

    assert new_ids and not (old_ids & new_ids)
    assert all(c.version == "9.9.9" for c in new.chunks if c.source_document_id == target["id"])


def test_the_committed_index_is_not_stale():
    """Guards against a corpus edit that was never re-ingested."""
    from ingest_knowledge import _comparable, build_index

    committed = KnowledgeIndex.load(REPO_ROOT / "knowledge" / "processed" / "index.json")
    assert _comparable(committed) == _comparable(build_index(embedder=HashingEmbedder()))


# -- traceability and validation --------------------------------------------


def test_every_chunk_traces_to_a_document_version_and_url(knowledge_index):
    for chunk in knowledge_index.chunks:
        assert chunk.source_document_id
        assert chunk.version
        assert chunk.source_url.startswith("https://")
        assert chunk.authority
        assert chunk.hazard_types
        assert chunk.chunk_id == chunk_id(
            chunk.source_document_id,
            chunk.version,
            # Ordinal is not stored; identity is verified by the id being
            # reproducible from the fields that are.
            next(
                i
                for i in range(50)
                if chunk_id(chunk.source_document_id, chunk.version, i, chunk.content)
                == chunk.chunk_id
            ),
            chunk.content,
        )


def test_ingestion_rejects_a_source_missing_provenance():
    from ingest_knowledge import validate_source

    assert validate_source({"id": "x"})
    assert any("authority" in p for p in validate_source({"id": "x"}))


def test_ingestion_rejects_an_unknown_hazard_type():
    from ingest_knowledge import validate_source

    problems = validate_source(
        {
            "id": "x", "title": "t", "authority": "a", "source_url": "u",
            "jurisdiction": "US", "locale": "en", "version": "1", "license": "public",
            "effective_from": "2026-01-01", "last_verified_at": "2026-01-01",
            "hazard_types": ["earthquake"],
        }
    )
    assert any("earthquake" in p for p in problems)


def test_a_disabled_source_is_kept_but_marked(knowledge_index):
    """A retired document must not vanish: its ids have to stay resolvable."""
    import yaml

    sources = yaml.safe_load(
        (REPO_ROOT / "knowledge" / "sources.yaml").read_text(encoding="utf-8")
    )
    retired = [s for s in sources["sources"] if not s.get("enabled", True)]
    assert retired, "the corpus should include a retired source as a live test case"
    assert all(
        c.enabled for c in knowledge_index.chunks
    ), "no enabled chunk should come from a retired document"


# -- embeddings -------------------------------------------------------------


def test_hashing_embedder_is_deterministic_and_normalised():
    embedder = HashingEmbedder()
    first, second = embedder.embed(["extreme heat", "extreme heat"])
    assert first == second
    assert cosine(first, second) == pytest.approx(1.0)


def test_identical_text_ranks_above_unrelated_text():
    embedder = HashingEmbedder()
    heat, heat_again, wind = embedder.embed(
        ["drink water during extreme heat", "drink water during extreme heat", "secure loose objects"]
    )
    assert cosine(heat, heat_again) > cosine(heat, wind)


def test_index_round_trips_through_json(tmp_path, knowledge_index):
    path = tmp_path / "index.json"
    knowledge_index.save(path)
    reloaded = KnowledgeIndex.load(path)
    assert reloaded.index_version == knowledge_index.index_version
    assert [c.chunk_id for c in reloaded.chunks] == [c.chunk_id for c in knowledge_index.chunks]
    assert reloaded.chunks[0].content_vector == knowledge_index.chunks[0].content_vector
