"""Extractor registry and lightweight semantic extractor contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

SEMANTIC_STATUSES = frozenset({"complete", "partial", "unsupported", "failed"})
SEMANTIC_RELATION_TYPES = frozenset(
    {
        "object_has_semantic_result",
        "semantic_mentions_entity",
        "semantic_mentions_place",
        "semantic_mentions_person",
        "semantic_mentions_topic",
    }
)


@dataclass(frozen=True)
class SemanticMention:
    relation_type: str
    target_ref: str
    confidence: float | None = None


@dataclass(frozen=True)
class SemanticOutput:
    status: str
    confidence: float | None = None
    result_json: dict[str, Any] | list[Any] | None = None
    result_text: str | None = None
    diagnostics: tuple[str, ...] = ()
    mentions: tuple[SemanticMention, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionContext:
    object_id: str
    object_type: str
    raw_uri: str
    content_hash: str
    blob_id: str | None
    canonical_metadata: dict[str, Any]
    derived_metadata: dict[str, Any] | None
    materialized_path: Path | None = None

    def raw_path(self) -> Path:
        if self.materialized_path is not None:
            return self.materialized_path
        parsed = urlparse(self.raw_uri)
        if parsed.scheme != "file":
            raise ValueError("extractor requires a file-backed RAW URI")
        text = unquote(parsed.path)
        if parsed.netloc:
            text = f"//{parsed.netloc}{text}"
        if len(text) >= 3 and text[0] == "/" and text[2] == ":":
            text = text[1:]
        return Path(text)

    def read_bytes(self) -> bytes:
        return self.raw_path().read_bytes()

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.raw_path().read_text(encoding=encoding)


ExtractorHandler = Callable[[ExtractionContext], SemanticOutput]


@dataclass(frozen=True)
class ExtractorSpec:
    name: str
    version: str
    semantic_type: str
    object_types: frozenset[str]
    handler: ExtractorHandler


@dataclass
class ExtractorRegistry:
    _specs: dict[tuple[str, str], ExtractorSpec] = field(default_factory=dict)

    def register(self, spec: ExtractorSpec) -> None:
        key = (spec.name, spec.version)
        if key in self._specs:
            raise ValueError(f"Extractor already registered: {spec.name}@{spec.version}")
        self._specs[key] = spec

    def get(self, name: str, version: str | None = None) -> ExtractorSpec:
        matches = [spec for key, spec in self._specs.items() if key[0] == name]
        if version is not None:
            matches = [spec for spec in matches if spec.version == version]
        if not matches:
            suffix = f"@{version}" if version else ""
            raise KeyError(f"Unknown extractor: {name}{suffix}")
        return sorted(matches, key=lambda spec: spec.version)[-1]

    def list(self) -> tuple[ExtractorSpec, ...]:
        return tuple(sorted(self._specs.values(), key=lambda spec: (spec.name, spec.version)))


def _unsupported(name: str) -> ExtractorHandler:
    def handler(_: ExtractionContext) -> SemanticOutput:
        return SemanticOutput(
            status="unsupported",
            diagnostics=(f"extractor_not_implemented:{name}",),
        )

    return handler


def _normalize_text(context: ExtractionContext) -> SemanticOutput:
    try:
        source = context.read_text()
    except (OSError, UnicodeError, ValueError) as exc:
        return SemanticOutput(
            status="unsupported",
            diagnostics=(f"text_source_unavailable:{type(exc).__name__}",),
        )
    normalized = re.sub(r"\s+", " ", source).strip()
    return SemanticOutput(
        status="complete",
        confidence=1.0,
        result_json={"character_count": len(normalized)},
        result_text=normalized,
    )


def build_default_registry() -> ExtractorRegistry:
    registry = ExtractorRegistry()
    definitions = {
        "photo": (
            "image_caption",
            "ocr_text",
            "detected_objects",
            "detected_places",
            "detected_people_candidates",
        ),
        "video": (
            "video_metadata_semantic",
            "keyframes",
            "speech_reference",
            "visual_summary",
        ),
        "audio": ("speech_to_text", "audio_summary"),
    }
    for object_type, names in definitions.items():
        for name in names:
            registry.register(
                ExtractorSpec(
                    name=name,
                    version="1.0.0",
                    semantic_type=name,
                    object_types=frozenset({object_type}),
                    handler=_unsupported(name),
                )
            )
    for name in ("text_normalization", "topics", "entities", "summary"):
        registry.register(
            ExtractorSpec(
                name=name,
                version="1.0.0",
                semantic_type=name,
                object_types=frozenset({"note", "message"}),
                handler=_normalize_text if name == "text_normalization" else _unsupported(name),
            )
        )
    return registry


DEFAULT_REGISTRY = build_default_registry()
