"""The knowledge index: what a retrievable chunk is, and how it is stored.

Runtime code reads a built index; it never parses `knowledge/sources.yaml`.
That file is an authoring artefact reviewed by a person, and keeping it out of
the request path means the Function App needs no YAML dependency and cannot be
affected by a half-edited source list.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"

# Hazard vocabulary shared by the rules, the sources file and the index. A
# trigger maps onto these; anything outside the set is a configuration error
# rather than a silently empty filter.
HAZARD_HEAT = "heat"
HAZARD_UV = "uv"
HAZARD_WIND = "wind"
HAZARD_RAIN = "rain"
HAZARD_FLOOD = "flood"
HAZARD_AIR_QUALITY = "air_quality"

HAZARD_TYPES = frozenset(
    {HAZARD_HEAT, HAZARD_UV, HAZARD_WIND, HAZARD_RAIN, HAZARD_FLOOD, HAZARD_AIR_QUALITY}
)


def chunk_id(source_document_id: str, version: str, ordinal: int, text: str) -> str:
    """A stable, content-addressed identifier.

    Stable so a citation stays resolvable, and content-addressed so an edited
    paragraph becomes a *different* chunk rather than silently changing what an
    old citation points at. Re-running ingestion on unchanged input therefore
    reproduces the same ids, which is what makes ingestion idempotent.
    """
    digest = hashlib.sha256(
        f"{source_document_id}|{version}|{ordinal}|{text.strip()}".encode()
    ).hexdigest()[:12]
    return f"{source_document_id}-{ordinal:03d}-{digest}"


def _parse_moment(value: str) -> datetime | None:
    """A date or a full timestamp, as UTC. None when absent or unparseable.

    `sources.yaml` carries plain dates, but a chunk that came back from Azure
    AI Search may carry a full timestamp. Both forms have to mean the same
    thing here, because this is the only place effectiveness is decided.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), time.min)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    source_document_id: str
    title: str
    content: str
    hazard_types: tuple[str, ...]
    severity: str
    authority: str
    jurisdiction: str
    locale: str
    effective_from: str
    last_verified_at: str
    source_url: str
    version: str
    enabled: bool = True
    # Empty means "no scheduled end". Guidance that is only valid for a season
    # or a named event can retire itself without an ingestion run.
    expires_at: str = ""
    heading: str = ""
    content_vector: tuple[float, ...] = field(default_factory=tuple)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self, *, include_vector: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        payload["hazard_types"] = list(self.hazard_types)
        if include_vector:
            payload["content_vector"] = list(self.content_vector)
        else:
            payload.pop("content_vector", None)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeChunk:
        return cls(
            chunk_id=payload["chunk_id"],
            source_document_id=payload["source_document_id"],
            title=payload.get("title", ""),
            content=payload.get("content", ""),
            hazard_types=tuple(payload.get("hazard_types", ())),
            severity=payload.get("severity", "info"),
            authority=payload.get("authority", ""),
            jurisdiction=payload.get("jurisdiction", ""),
            locale=payload.get("locale", "en"),
            effective_from=payload.get("effective_from", ""),
            last_verified_at=payload.get("last_verified_at", ""),
            source_url=payload.get("source_url", ""),
            version=payload.get("version", ""),
            enabled=bool(payload.get("enabled", True)),
            expires_at=payload.get("expires_at", "") or "",
            heading=payload.get("heading", ""),
            content_vector=tuple(payload.get("content_vector", ())),
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
        )

    def is_effective(self, now: datetime | None = None) -> bool:
        """Whether this chunk may be cited right now.

        Disabled, not-yet-effective and expired are all excluded here rather
        than at query time, so no caller can forget the check.
        """
        if not self.enabled:
            return False
        moment = now or datetime.now(UTC)
        starts = _parse_moment(self.effective_from)
        if starts is not None and starts > moment:
            return False
        ends = _parse_moment(self.expires_at)
        return not (ends is not None and ends <= moment)


@dataclass(frozen=True)
class KnowledgeIndex:
    """An immutable snapshot of the corpus, identified by `index_version`.

    The version participates in every cache key, which is how a re-ingest
    invalidates retrieval and generation caches without anything having to
    remember to flush them.
    """

    index_version: str
    chunks: tuple[KnowledgeChunk, ...]
    embedding_model: str = ""
    built_at_utc: str = ""

    def enabled_chunks(self, now: datetime | None = None) -> tuple[KnowledgeChunk, ...]:
        return tuple(c for c in self.chunks if c.is_effective(now))

    def by_id(self, identifier: str) -> KnowledgeChunk | None:
        for chunk in self.chunks:
            if chunk.chunk_id == identifier:
                return chunk
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_version": self.index_version,
            "embedding_model": self.embedding_model,
            "built_at_utc": self.built_at_utc,
            "schema_version": SCHEMA_VERSION,
            "chunks": [c.to_dict() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> KnowledgeIndex:
        return cls(
            index_version=payload.get("index_version", "unknown"),
            embedding_model=payload.get("embedding_model", ""),
            built_at_utc=payload.get("built_at_utc", ""),
            chunks=tuple(KnowledgeChunk.from_dict(c) for c in payload.get("chunks", [])),
        )

    @classmethod
    def load(cls, path: str | Path) -> KnowledgeIndex:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
