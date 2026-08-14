"""Read-only deterministic retrieval over canonical and derived Human OS INDEX data."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


SEARCH_VERSION = "1.0.0"
OBJECT_ID_PATTERN = re.compile(r"^hos_obj_[0-9a-f]{32}$")

_STOP_WORDS = {
    "a", "an", "and", "find", "in", "of", "on", "photo", "photos", "the",
    "with", "в", "где", "и", "на", "найди", "с", "со", "фото", "фотографии",
}

# This is deliberately small and auditable. It bridges common Russian query words to
# the English labels emitted by PHOTO v1; it is not an inference model.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "snow": ("snow", "snowy", "снег", "снегу", "снегом", "снеж"),
    "person": (
        "person", "people", "man", "men", "woman", "women", "boy", "girl",
        "human", "человек", "люди", "мужчина", "женщина", "меня",
    ),
    "swimwear": (
        "swimwear", "swimsuit", "swimming trunks", "bathing suit", "trunks",
        "плавки", "плавках", "купальник",
    ),
    "signboard": (
        "sign", "signboard", "signage", "board", "billboard", "text", "lettering",
        "вывеска", "вывеской", "табличка",
    ),
    "car": (
        "car", "automobile", "vehicle", "машина", "машиной", "автомобиль",
    ),
    "sea": (
        "sea", "ocean", "seaside", "море", "морем", "океан",
    ),
}


@dataclass(frozen=True)
class SearchFilters:
    time_from: str | None = None
    time_to: str | None = None
    place: str | None = None
    media_type: str | None = "photo"


@dataclass(frozen=True)
class SearchEvidence:
    evidence_id: str
    evidence_kind: str
    semantic_result_id: str | None
    semantic_type: str
    extractor_name: str
    extractor_version: str
    source_blob_id: str | None
    source_content_hash: str
    confidence: float | None
    matched_concepts: tuple[str, ...]
    snippet: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class SearchHit:
    object_id: str
    object_type: str
    raw_uri: str
    occurred_at: str | None
    captured_at: str | None
    score: float
    reason: str
    matched_concepts: tuple[str, ...]
    missing_concepts: tuple[str, ...]
    evidence: tuple[SearchEvidence, ...]


@dataclass(frozen=True)
class SearchResponse:
    query: str
    search_version: str
    filters: SearchFilters
    requested_concepts: tuple[str, ...]
    unsupported_concepts: tuple[str, ...]
    best_effort: bool
    explanation: str | None
    results: tuple[SearchHit, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE).strip()


def _tokens(text: str) -> list[str]:
    return [token for token in _normalize(text).split() if token not in _STOP_WORDS]


def _query_concepts(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = _normalize(query)
    tokens = _tokens(query)
    concepts: list[str] = []
    consumed: set[str] = set()
    for concept, aliases in _CONCEPTS.items():
        if any(_normalize(alias) in normalized for alias in aliases):
            concepts.append(concept)
            for alias in aliases:
                alias_norm = _normalize(alias)
                if alias_norm in normalized:
                    consumed.update(alias_norm.split())
    free_terms = [token for token in tokens if token not in consumed]
    concepts.extend(term for term in free_terms if term not in concepts)
    return tuple(concepts), tuple(free_terms)


def _concept_aliases(concept: str) -> tuple[str, ...]:
    return _CONCEPTS.get(concept, (concept,))


def _contains_alias(text: str, alias: str) -> bool:
    haystack = f" {_normalize(text)} "
    needle = _normalize(alias)
    if not needle:
        return False
    if " " in needle:
        return f" {needle} " in haystack
    return re.search(rf"(?<!\w){re.escape(needle)}(?:\w*)?(?!\w)", haystack) is not None


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_time_range(value: str | None, start: str | None, end: str | None) -> bool:
    if start is None and end is None:
        return True
    current = _parse_time(value)
    if current is None:
        return False
    lower, upper = _parse_time(start), _parse_time(end)
    if start is not None and lower is None:
        raise ValueError(f"invalid time_from: {start}")
    if end is not None and upper is None:
        raise ValueError(f"invalid time_to: {end}")
    try:
        return (lower is None or current >= lower) and (upper is None or current <= upper)
    except TypeError as exc:
        raise ValueError("time filters and object timestamps must use compatible offsets") from exc


def _evidence_text(row: sqlite3.Row, relations: Sequence[sqlite3.Row]) -> str:
    parts = [row["result_text"] or "", row["result_json"] or ""]
    parts.extend(relation["target_ref"] for relation in relations)
    return " ".join(parts)


def _snippet(row: sqlite3.Row) -> str:
    if row["result_text"]:
        return re.sub(r"\s+", " ", row["result_text"]).strip()[:320]
    payload = _decode_json(row["result_json"], {})
    if row["semantic_type"] == "detected_objects":
        labels = [item.get("label") for item in payload.get("detections", [])]
        return "detected objects: " + ", ".join(str(label) for label in labels if label)
    if row["semantic_type"] == "detected_places":
        return "place candidates: " + json.dumps(
            payload.get("candidates", []), ensure_ascii=False, separators=(",", ":")
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:320]


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def safe_object_ref(object_id: str) -> str:
    """Return an opaque reference that never exposes the backing RAW location."""
    return f"human-os://object/{object_id}"


def retrieval_health(database_path: Path) -> dict[str, Any]:
    """Check read-only INDEX availability without running extractors or migrations."""
    with _open_read_only(database_path) as conn:
        conn.execute("SELECT 1 FROM objects LIMIT 1").fetchall()
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {
        "status": "ok",
        "read_only": True,
        "search_version": SEARCH_VERSION,
        "schema_version": schema_version,
    }


def get_object_record(object_id: str, database_path: Path) -> dict[str, Any] | None:
    """Return one safe canonical object view and its current derived evidence."""
    if not OBJECT_ID_PATTERN.fullmatch(object_id):
        raise ValueError("invalid object_id")
    with _open_read_only(database_path) as conn:
        row = conn.execute(
            """
            SELECT o.object_id, o.object_type, o.occurred_at, o.captured_at,
                   o.content_hash, o.mime_type, a.blob_id
            FROM objects o
            LEFT JOIN attachments a ON a.object_id = o.object_id
            WHERE o.object_id = ?
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            return None
        semantic_rows = conn.execute(
            """
            SELECT semantic_result_id, semantic_type, extractor_name,
                   extractor_version, status, confidence, result_text,
                   result_json, source_blob_id, source_content_hash, created_at
            FROM semantic_results
            WHERE object_id = ? AND is_current = 1
            ORDER BY semantic_type, semantic_result_id
            """,
            (object_id,),
        ).fetchall()
    evidence = []
    for semantic in semantic_rows:
        evidence.append(
            {
                "semantic_result_id": semantic["semantic_result_id"],
                "semantic_type": semantic["semantic_type"],
                "extractor_name": semantic["extractor_name"],
                "extractor_version": semantic["extractor_version"],
                "status": semantic["status"],
                "confidence": semantic["confidence"],
                "text": semantic["result_text"],
                "result": _decode_json(semantic["result_json"], None),
                "provenance": {
                    "source_blob_id": semantic["source_blob_id"],
                    "source_content_hash": semantic["source_content_hash"],
                    "created_at": semantic["created_at"],
                },
                "assertion": "derived_evidence",
            }
        )
    return {
        "object_id": row["object_id"],
        "object_type": row["object_type"],
        "occurred_at": row["occurred_at"],
        "captured_at": row["captured_at"],
        "mime_type": row["mime_type"],
        "safe_ref": safe_object_ref(row["object_id"]),
        "provenance": {
            "blob_id": row["blob_id"],
            "content_hash": row["content_hash"],
        },
        "evidence": evidence,
    }


def _load_candidates(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT o.object_id, o.object_type, o.raw_uri, o.occurred_at, o.captured_at,
               o.metadata_json, o.content_hash, a.blob_id,
               s.semantic_result_id, s.semantic_type,
               s.extractor_name, s.extractor_version, s.source_blob_id,
               s.source_content_hash, s.confidence, s.result_text, s.result_json,
               s.provenance_json
        FROM objects o
        LEFT JOIN attachments a ON a.object_id = o.object_id
        JOIN semantic_results s ON s.object_id = o.object_id
        WHERE s.is_current = 1 AND s.status IN ('complete', 'partial')
        ORDER BY o.object_id, s.semantic_result_id
        """
    ).fetchall()
    relation_rows = conn.execute(
        """
        SELECT semantic_result_id, relation_type, target_ref, confidence
        FROM semantic_relations ORDER BY semantic_result_id, semantic_relation_id
        """
    ).fetchall()
    by_result: dict[str, list[sqlite3.Row]] = {}
    for relation in relation_rows:
        by_result.setdefault(relation["semantic_result_id"], []).append(relation)
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = candidates.setdefault(
            row["object_id"],
            {
                "object_id": row["object_id"], "object_type": row["object_type"],
                "raw_uri": row["raw_uri"], "occurred_at": row["occurred_at"],
                "captured_at": row["captured_at"], "metadata_json": row["metadata_json"],
                "content_hash": row["content_hash"], "blob_id": row["blob_id"],
                "results": [], "canonical_evidence": [],
            },
        )
        item["results"].append((row, by_result.get(row["semantic_result_id"], [])))
    metadata_rows = conn.execute(
        """
        SELECT object_id, extraction_id, extractor_id, metadata_json, extracted_at
        FROM metadata_extractions ORDER BY object_id, extracted_at DESC, extraction_id DESC
        """
    ).fetchall()
    seen_metadata: set[str] = set()
    for row in metadata_rows:
        if row["object_id"] in candidates and row["object_id"] not in seen_metadata:
            candidates[row["object_id"]]["canonical_evidence"].append(
                {
                    "evidence_id": row["extraction_id"], "evidence_kind": "derived_metadata",
                    "semantic_type": "media_metadata", "extractor_name": row["extractor_id"],
                    "text": row["metadata_json"], "provenance": {
                        "table": "metadata_extractions", "extraction_id": row["extraction_id"]
                    },
                }
            )
            seen_metadata.add(row["object_id"])
    relation_rows = conn.execute(
        """
        SELECT relation_id, from_object_id, to_object_id, relation_type, confidence, model_id
        FROM relations ORDER BY relation_id
        """
    ).fetchall()
    for row in relation_rows:
        for object_id in {row["from_object_id"], row["to_object_id"]}:
            if object_id in candidates:
                candidates[object_id]["canonical_evidence"].append(
                    {
                        "evidence_id": row["relation_id"], "evidence_kind": "object_relation",
                        "semantic_type": "object_relation", "extractor_name": row["model_id"] or "canonical-index",
                        "text": f"{row['relation_type']} {row['from_object_id']} {row['to_object_id']}",
                        "confidence": row["confidence"], "provenance": {
                            "table": "relations", "relation_id": row["relation_id"]
                        },
                    }
                )
    return candidates


def _match_result(
    row: sqlite3.Row, relations: Sequence[sqlite3.Row], concepts: Sequence[str]
) -> tuple[str, ...]:
    text = _evidence_text(row, relations)
    return tuple(
        concept
        for concept in concepts
        if any(_contains_alias(text, alias) for alias in _concept_aliases(concept))
    )


def search_index(
    query: str,
    database_path: Path,
    *,
    filters: SearchFilters | None = None,
    limit: int = 10,
) -> SearchResponse:
    """Search current derived evidence without mutating or recomputing INDEX data."""
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    filters = filters or SearchFilters()
    concepts, _ = _query_concepts(query)
    if not concepts:
        raise ValueError("query has no searchable terms")

    with _open_read_only(database_path) as conn:
        candidates = _load_candidates(conn)

    hits: list[SearchHit] = []
    globally_matched: set[str] = set()
    for item in candidates.values():
        if filters.media_type and item["object_type"] != filters.media_type:
            continue
        timestamp = item["occurred_at"] or item["captured_at"]
        if not _in_time_range(timestamp, filters.time_from, filters.time_to):
            continue
        matched: set[str] = set()
        evidence: list[SearchEvidence] = []
        for row, relations in item["results"]:
            result_matches = _match_result(row, relations, concepts)
            if not result_matches:
                continue
            matched.update(result_matches)
            evidence.append(
                SearchEvidence(
                    evidence_id=row["semantic_result_id"],
                    evidence_kind="semantic_result",
                    semantic_result_id=row["semantic_result_id"],
                    semantic_type=row["semantic_type"],
                    extractor_name=row["extractor_name"],
                    extractor_version=row["extractor_version"],
                    source_blob_id=row["source_blob_id"],
                    source_content_hash=row["source_content_hash"],
                    confidence=row["confidence"],
                    matched_concepts=result_matches,
                    snippet=_snippet(row),
                    provenance=_decode_json(row["provenance_json"], {}),
                )
            )
        canonical_metadata = {
            "evidence_id": item["object_id"], "evidence_kind": "canonical_metadata",
            "semantic_type": "canonical_metadata", "extractor_name": "canonical-index",
            "text": item["metadata_json"], "provenance": {
                "table": "objects", "object_id": item["object_id"]
            },
        }
        for derived in (canonical_metadata, *item["canonical_evidence"]):
            derived_matches = tuple(
                concept for concept in concepts
                if any(_contains_alias(derived["text"], alias) for alias in _concept_aliases(concept))
            )
            if not derived_matches:
                continue
            matched.update(derived_matches)
            evidence.append(
                SearchEvidence(
                    evidence_id=derived["evidence_id"], evidence_kind=derived["evidence_kind"],
                    semantic_result_id=None, semantic_type=derived["semantic_type"],
                    extractor_name=derived["extractor_name"], extractor_version="n/a",
                    source_blob_id=item["blob_id"], source_content_hash=item["content_hash"],
                    confidence=derived.get("confidence"), matched_concepts=derived_matches,
                    snippet=str(derived["text"])[:320], provenance=derived["provenance"],
                )
            )
        if filters.place:
            place_text = " ".join(
                _evidence_text(row, relations)
                for row, relations in item["results"]
                if row["semantic_type"] == "detected_places"
            )
            place_text += " " + " ".join(
                value["text"] for value in item["canonical_evidence"]
                if value["evidence_kind"] == "derived_metadata"
            )
            if not all(_contains_alias(place_text, term) for term in _tokens(filters.place)):
                continue
        if not matched:
            continue
        globally_matched.update(matched)
        coverage = len(matched) / len(concepts)
        confidence_values = [value.confidence for value in evidence if value.confidence is not None]
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.5
        # Coverage dominates confidence and evidence count, yielding deterministic ranking.
        score = round(0.8 * coverage + 0.15 * confidence + 0.05 * min(len(evidence), 3) / 3, 6)
        missing = tuple(concept for concept in concepts if concept not in matched)
        reason = f"matched {len(matched)}/{len(concepts)} concepts: {', '.join(sorted(matched))}"
        if missing:
            reason += f"; missing derived evidence for: {', '.join(missing)}"
        hits.append(
            SearchHit(
                object_id=item["object_id"], object_type=item["object_type"],
                raw_uri=item["raw_uri"], occurred_at=item["occurred_at"],
                captured_at=item["captured_at"], score=score, reason=reason,
                matched_concepts=tuple(sorted(matched)), missing_concepts=missing,
                evidence=tuple(evidence),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.object_id))
    hits = hits[:limit]
    unsupported = tuple(concept for concept in concepts if concept not in globally_matched)
    best_effort = bool(unsupported) or (bool(hits) and not any(not hit.missing_concepts for hit in hits))
    explanation = None
    if unsupported:
        explanation = (
            "Current derived semantic evidence cannot verify: "
            + ", ".join(unsupported)
            + ". Results are the closest candidates and must not be treated as canonical truth."
        )
    return SearchResponse(
        query=query, search_version=SEARCH_VERSION, filters=filters,
        requested_concepts=concepts, unsupported_concepts=unsupported,
        best_effort=best_effort, explanation=explanation, results=tuple(hits),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--time-from")
    parser.add_argument("--time-to")
    parser.add_argument("--place")
    parser.add_argument("--media-type", default="photo")
    parser.add_argument("--limit", type=int, default=10)
    return parser


def main(argv: Iterable[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    response = search_index(
        args.query, args.db,
        filters=SearchFilters(args.time_from, args.time_to, args.place, args.media_type),
        limit=args.limit,
    )
    print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
