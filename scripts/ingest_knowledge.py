"""Build the knowledge index from `knowledge/sources.yaml` and `knowledge/raw/`.

    python scripts/ingest_knowledge.py            # build knowledge/processed/index.json
    python scripts/ingest_knowledge.py --check    # fail if the committed index is stale

Idempotent by construction: chunk ids are content-addressed, so re-running on
unchanged input reproduces the same index byte for byte. A document changes
only when its `version` changes in sources.yaml, and then *all* of its chunks
are replaced — there is no partial-update path that could leave two versions of
the same passage in the index.

Disabled sources are still ingested, with `enabled: false`. Dropping them
entirely would make a withdrawn document's chunk ids resolvable to nothing,
and a citation that dangles is worse than one that is explicitly retired.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "functions"))

from weather.advice.chunking import chunk_document  # noqa: E402
from weather.advice.embeddings import HashingEmbedder, get_embedder  # noqa: E402
from weather.advice.knowledge import (  # noqa: E402
    HAZARD_TYPES,
    KnowledgeChunk,
    KnowledgeIndex,
    chunk_id,
)

SOURCES = REPO / "knowledge" / "sources.yaml"
RAW = REPO / "knowledge" / "raw"
OUTPUT = REPO / "knowledge" / "processed" / "index.json"

# Severity of the guidance itself, not of the weather. Used to prefer the
# passage that matches how serious the current reading is.
SEVERITY_BY_HAZARD = {
    "heat": "warning",
    "uv": "warning",
    "wind": "warning",
    "rain": "info",
    "flood": "severe",
    "air_quality": "info",
}


def load_sources(path: Path = SOURCES) -> tuple[str, list[dict]]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - dev dependency
        raise SystemExit(
            "PyYAML is required for ingestion: pip install -r requirements-dev.txt"
        ) from None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload.get("index_version", "unknown"), payload.get("sources", [])


def validate_source(source: dict) -> list[str]:
    """Reject a source that cannot be cited responsibly."""
    problems = []
    required = (
        "id", "title", "authority", "source_url", "jurisdiction", "locale",
        "hazard_types", "version", "effective_from", "last_verified_at", "license",
    )
    for key in required:
        if not source.get(key):
            problems.append(f"{source.get('id', '<no id>')}: missing '{key}'")
    unknown = set(source.get("hazard_types") or []) - HAZARD_TYPES
    if unknown:
        problems.append(f"{source.get('id')}: unknown hazard types {sorted(unknown)}")
    return problems


def build_index(
    sources_path: Path = SOURCES, raw_dir: Path = RAW, embedder=None
) -> KnowledgeIndex:
    index_version, sources = load_sources(sources_path)

    problems = [p for source in sources for p in validate_source(source)]
    if problems:
        raise SystemExit("Invalid sources.yaml:\n  " + "\n  ".join(problems))

    embedder = embedder or get_embedder()
    chunks: list[KnowledgeChunk] = []
    seen: set[str] = set()

    for source in sources:
        document = raw_dir / f"{source['id']}.md"
        if not document.exists():
            if not source.get("enabled", True):
                # A retired source may have had its text removed; the registry
                # entry alone is enough to keep its history explicit.
                continue
            raise SystemExit(f"{source['id']}: enabled but {document} is missing")

        hazards = tuple(source["hazard_types"])
        severity = SEVERITY_BY_HAZARD.get(hazards[0], "info")

        for ordinal, section in enumerate(chunk_document(document.read_text(encoding="utf-8"))):
            identifier = chunk_id(source["id"], source["version"], ordinal, section.text)
            if identifier in seen:
                continue
            seen.add(identifier)
            chunks.append(
                KnowledgeChunk(
                    chunk_id=identifier,
                    source_document_id=source["id"],
                    title=source["title"],
                    content=section.text,
                    heading=section.heading,
                    hazard_types=hazards,
                    severity=severity,
                    authority=source["authority"],
                    jurisdiction=source["jurisdiction"],
                    locale=source["locale"],
                    effective_from=str(source["effective_from"]),
                    last_verified_at=str(source["last_verified_at"]),
                    source_url=source["source_url"],
                    version=str(source["version"]),
                    enabled=bool(source.get("enabled", True)),
                )
            )

    vectors = embedder.embed([f"{c.heading}\n{c.content}" for c in chunks]) if chunks else []
    chunks = [
        KnowledgeChunk(**{**c.to_dict(include_vector=False), "content_vector": tuple(vector)})
        for c, vector in zip(chunks, vectors, strict=True)
    ]

    return KnowledgeIndex(
        index_version=index_version,
        chunks=tuple(chunks),
        embedding_model=embedder.name,
        # Deliberately not a timestamp when checking: see `--check`.
        built_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


def _comparable(index: KnowledgeIndex) -> str:
    payload = index.to_dict()
    payload.pop("built_at_utc", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed index differs from a fresh build.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force the hashing embedder even when Azure OpenAI is configured.",
    )
    args = parser.parse_args()

    index = build_index(embedder=HashingEmbedder() if args.offline else None)
    destination = Path(args.output)

    if args.check:
        if not destination.exists():
            print(f"{destination} does not exist; run ingestion.")
            return 1
        if _comparable(KnowledgeIndex.load(destination)) != _comparable(index):
            print(f"{destination} is stale; re-run scripts/ingest_knowledge.py")
            return 1
        print(f"{destination} is up to date ({len(index.chunks)} chunks).")
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    index.save(destination)

    by_document: dict[str, int] = {}
    for chunk in index.chunks:
        by_document[chunk.source_document_id] = by_document.get(chunk.source_document_id, 0) + 1

    print(f"index_version {index.index_version}  embedder {index.embedding_model}")
    for document, count in sorted(by_document.items()):
        enabled = any(
            c.enabled for c in index.chunks if c.source_document_id == document
        )
        print(f"  {document:26} {count:3} chunks{'' if enabled else '   (disabled)'}")
    print(f"wrote {destination.relative_to(REPO)} — {len(index.chunks)} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
