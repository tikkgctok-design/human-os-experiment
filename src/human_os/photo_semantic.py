"""Production local PHOTO semantic extractors for Human OS INDEX v5."""

from __future__ import annotations

import importlib.metadata
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps

from .metadata_extraction import extract_media_metadata
from .semantic_extraction import SemanticRunResult, run_semantic_extractor
from .semantic_registry import (
    ExtractionContext,
    ExtractorRegistry,
    ExtractorSpec,
    SemanticMention,
    SemanticOutput,
)

PHOTO_EXTRACTOR_VERSION = "1.0.0"
OBJECTS_EXTRACTOR_VERSION = "2.0.1"
CAPTION_EXTRACTOR = "human-os.photo.florence2.caption"
OCR_EXTRACTOR = "human-os.photo.florence2.ocr"
OBJECTS_EXTRACTOR = "human-os.photo.florence2.objects"
PLACE_EXTRACTOR = "human-os.photo.metadata.place-candidates"
PHOTO_EXTRACTORS = (
    CAPTION_EXTRACTOR,
    OCR_EXTRACTOR,
    OBJECTS_EXTRACTOR,
    PLACE_EXTRACTOR,
)

FLORENCE_MODEL_ID = "florence-community/Florence-2-base-ft"
FLORENCE_MODEL_REVISION = "0b03b6f15a4a211370fb204aee4e7dd48887ea37"
DETR_MODEL_ID = "facebook/detr-resnet-50"
DETR_MODEL_REVISION = "70120ba84d68ca1211e007c4fb61d0cd5424be54"


@dataclass(frozen=True)
class PhotoVisionResult:
    task: str
    parsed: Any
    runtime: dict[str, Any]


class PhotoVisionBackend(Protocol):
    def analyze(self, path: Path, task: str) -> PhotoVisionResult: ...


@dataclass(frozen=True)
class Florence2Config:
    model_id: str = FLORENCE_MODEL_ID
    model_revision: str = FLORENCE_MODEL_REVISION
    device: str = "auto"
    dtype: str = "auto"
    num_beams: int = 3
    max_new_tokens: int = 1024
    do_sample: bool = False
    local_files_only: bool = False

    def generation_parameters(self) -> dict[str, Any]:
        return {
            "num_beams": self.num_beams,
            "max_new_tokens": self.max_new_tokens,
            "do_sample": self.do_sample,
        }


class Florence2Backend:
    """Lazy, pinned Florence-2 runtime; no model is loaded at import time."""

    def __init__(self, config: Florence2Config | None = None) -> None:
        self.config = config or Florence2Config()
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._device: str | None = None
        self._dtype: Any = None

    @staticmethod
    def _package_version(name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def _resolved_runtime(self) -> tuple[Any, str, Any]:
        try:
            import torch
            from transformers import (
                Florence2ForConditionalGeneration,
                Florence2Processor,
            )
        except ImportError as exc:
            raise RuntimeError(
                "PHOTO semantic runtime is not installed; install the 'vision' extra"
            ) from exc

        device = self.config.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype_name = self.config.dtype
        if dtype_name == "auto":
            dtype_name = "float16" if device == "cuda" else "float32"
        if not hasattr(torch, dtype_name):
            raise ValueError(f"unsupported torch dtype: {dtype_name}")
        dtype = getattr(torch, dtype_name)

        processor = Florence2Processor.from_pretrained(
            self.config.model_id,
            revision=self.config.model_revision,
            local_files_only=self.config.local_files_only,
            use_fast=False,
        )
        model = Florence2ForConditionalGeneration.from_pretrained(
            self.config.model_id,
            revision=self.config.model_revision,
            local_files_only=self.config.local_files_only,
            dtype=dtype,
            attn_implementation="eager",
        )
        model = model.to(device).eval()
        self._torch = torch
        self._processor = processor
        self._model = model
        self._device = device
        self._dtype = dtype
        return torch, device, dtype

    def _ensure_loaded(self) -> None:
        if self._model is None:
            self._resolved_runtime()

    def runtime_provenance(self, task: str) -> dict[str, Any]:
        device = self._device or self.config.device
        dtype = str(self._dtype).removeprefix("torch.") if self._dtype else self.config.dtype
        return {
            "backend": "transformers-native-florence2",
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "task": task,
            "device": device,
            "dtype": dtype,
            "parameters": {
                **self.config.generation_parameters(),
                "attention_implementation": "eager",
                "fast_image_processor": False,
            },
            "runtime": {
                "python": platform.python_version(),
                "torch": self._package_version("torch"),
                "transformers": self._package_version("transformers"),
                "timm": self._package_version("timm"),
            },
        }

    def analyze(self, path: Path, task: str) -> PhotoVisionResult:
        self._ensure_loaded()
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image_size = image.size
            inputs = self._processor(text=task, images=image, return_tensors="pt")
        moved: dict[str, Any] = {}
        for key, value in inputs.items():
            if hasattr(value, "is_floating_point") and value.is_floating_point():
                moved[key] = value.to(self._device, dtype=self._dtype)
            else:
                moved[key] = value.to(self._device)
        with self._torch.inference_mode():
            generated_ids = self._model.generate(
                **moved,
                **self.config.generation_parameters(),
            )
        generated_text = self._processor.batch_decode(
            generated_ids, skip_special_tokens=False
        )[0]
        parsed = self._processor.post_process_generation(
            generated_text,
            task=task,
            image_size=image_size,
        )
        return PhotoVisionResult(
            task=task,
            parsed=parsed,
            runtime=self.runtime_provenance(task),
        )


@dataclass(frozen=True)
class DetrConfig:
    model_id: str = DETR_MODEL_ID
    model_revision: str = DETR_MODEL_REVISION
    device: str = "auto"
    threshold: float = 0.7
    local_files_only: bool = False


class DetrObjectBackend:
    """High-precision local COCO object detector with calibrated scores."""

    def __init__(self, config: DetrConfig | None = None) -> None:
        self.config = config or DetrConfig()
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._device: str | None = None

    @staticmethod
    def _package_version(name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import DetrForObjectDetection, DetrImageProcessor
        except ImportError as exc:
            raise RuntimeError(
                "PHOTO semantic runtime is not installed; install the 'vision' extra"
            ) from exc
        device = self.config.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._processor = DetrImageProcessor.from_pretrained(
            self.config.model_id,
            revision=self.config.model_revision,
            local_files_only=self.config.local_files_only,
        )
        self._model = DetrForObjectDetection.from_pretrained(
            self.config.model_id,
            revision=self.config.model_revision,
            local_files_only=self.config.local_files_only,
        ).to(device).eval()
        self._torch = torch
        self._device = device

    def runtime_provenance(self, task: str) -> dict[str, Any]:
        return {
            "backend": "transformers-native-detr",
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "task": task,
            "device": self._device or self.config.device,
            "dtype": "float32",
            "parameters": {"threshold": self.config.threshold},
            "runtime": {
                "python": platform.python_version(),
                "torch": self._package_version("torch"),
                "transformers": self._package_version("transformers"),
            },
        }

    def analyze(self, path: Path, task: str) -> PhotoVisionResult:
        if task != "<OD>":
            raise ValueError(f"DETR backend does not support task: {task}")
        self._ensure_loaded()
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            target_sizes = self._torch.tensor([image.size[::-1]], device=self._device)
            inputs = self._processor(images=image, return_tensors="pt")
        inputs = {key: value.to(self._device) for key, value in inputs.items()}
        with self._torch.inference_mode():
            outputs = self._model(**inputs)
        result = self._processor.post_process_object_detection(
            outputs,
            threshold=self.config.threshold,
            target_sizes=target_sizes,
        )[0]
        labels = [
            self._model.config.id2label[int(label)] for label in result["labels"]
        ]
        parsed = {
            "<OD>": {
                "labels": labels,
                "bboxes": result["boxes"].detach().cpu().tolist(),
                "scores": result["scores"].detach().cpu().tolist(),
            }
        }
        return PhotoVisionResult(
            task=task,
            parsed=parsed,
            runtime=self.runtime_provenance(task),
        )


class ProductionPhotoBackend:
    """Route caption/OCR to Florence-2 and object detection to DETR."""

    def __init__(
        self,
        florence: Florence2Backend | None = None,
        detr: DetrObjectBackend | None = None,
    ) -> None:
        self.florence = florence or Florence2Backend()
        self.detr = detr or DetrObjectBackend()

    def analyze(self, path: Path, task: str) -> PhotoVisionResult:
        if task == "<OD>":
            return self.detr.analyze(path, task)
        return self.florence.analyze(path, task)

    def runtime_provenance(self, task: str) -> dict[str, Any]:
        if task == "<OD>":
            return self.detr.runtime_provenance(task)
        return self.florence.runtime_provenance(task)


def _backend_provenance(backend: PhotoVisionBackend, task: str) -> dict[str, Any]:
    describe = getattr(backend, "runtime_provenance", None)
    if callable(describe):
        return describe(task)
    return {"backend": type(backend).__name__, "task": task}


def _failed(backend: PhotoVisionBackend, task: str, exc: Exception) -> SemanticOutput:
    return SemanticOutput(
        status="failed",
        diagnostics=(f"photo_semantic_failed:{type(exc).__name__}: {exc}",),
        provenance=_backend_provenance(backend, task),
    )


def _task_payload(parsed: Any, task: str) -> Any:
    if isinstance(parsed, dict) and task in parsed:
        return parsed[task]
    return parsed


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _caption_handler(backend: PhotoVisionBackend):
    task = "<DETAILED_CAPTION>"

    def handler(context: ExtractionContext) -> SemanticOutput:
        try:
            vision = backend.analyze(context.raw_path(), task)
            caption = _text(_task_payload(vision.parsed, task))
        except Exception as exc:
            return _failed(backend, task, exc)
        diagnostics = () if caption else ("caption_empty",)
        return SemanticOutput(
            status="complete" if caption else "partial",
            result_json={"caption": caption, "task": task},
            result_text=caption,
            diagnostics=diagnostics + ("model_confidence_unavailable",),
            provenance=vision.runtime,
        )

    return handler


def _ocr_handler(backend: PhotoVisionBackend):
    task = "<OCR_WITH_REGION>"

    def handler(context: ExtractionContext) -> SemanticOutput:
        try:
            vision = backend.analyze(context.raw_path(), task)
            payload = _task_payload(vision.parsed, task)
            if isinstance(payload, dict):
                labels = [_text(value) for value in payload.get("labels", [])]
                labels = [value for value in labels if value]
                boxes = payload.get("quad_boxes", [])
                scores = payload.get("scores", [])
            else:
                text = _text(payload)
                labels, boxes, scores = ([text] if text else []), [], []
        except Exception as exc:
            return _failed(backend, task, exc)
        regions = []
        for index, label in enumerate(labels):
            region: dict[str, Any] = {"text": label}
            if index < len(boxes):
                region["quad_box"] = boxes[index]
            if index < len(scores) and isinstance(scores[index], (int, float)):
                region["confidence"] = round(float(scores[index]), 6)
            regions.append(region)
        numeric_scores = [
            float(value) for value in scores if isinstance(value, (int, float))
        ]
        confidence = (
            round(sum(numeric_scores) / len(numeric_scores), 6)
            if numeric_scores
            else None
        )
        diagnostics = () if confidence is not None else ("model_confidence_unavailable",)
        return SemanticOutput(
            status="complete",
            confidence=confidence,
            result_json={"regions": regions, "task": task},
            result_text="\n".join(labels),
            diagnostics=diagnostics,
            provenance=vision.runtime,
        )

    return handler


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "unknown"


def _objects_handler(backend: PhotoVisionBackend):
    task = "<OD>"

    def handler(context: ExtractionContext) -> SemanticOutput:
        try:
            vision = backend.analyze(context.raw_path(), task)
            payload = _task_payload(vision.parsed, task)
            payload = payload if isinstance(payload, dict) else {}
            labels = [_text(value) for value in payload.get("labels", [])]
            boxes = payload.get("bboxes", [])
            scores = payload.get("scores", [])
        except Exception as exc:
            return _failed(backend, task, exc)

        with Image.open(context.raw_path()) as image:
            width, height = image.size
        detections: list[dict[str, Any]] = []
        numeric_scores: list[float] = []
        for index, label in enumerate(labels):
            if not label:
                continue
            detection: dict[str, Any] = {"label": label}
            if index < len(boxes) and len(boxes[index]) == 4:
                box = [round(float(value), 3) for value in boxes[index]]
                detection["bbox"] = box
                detection["bbox_normalized"] = [
                    round(box[0] / width, 6),
                    round(box[1] / height, 6),
                    round(box[2] / width, 6),
                    round(box[3] / height, 6),
                ]
            if index < len(scores) and isinstance(scores[index], (int, float)):
                score = round(float(scores[index]), 6)
                detection["confidence"] = score
                numeric_scores.append(score)
            detections.append(detection)
        confidence = (
            round(sum(numeric_scores) / len(numeric_scores), 6)
            if numeric_scores
            else None
        )
        mentions_list: list[SemanticMention] = []
        for label in dict.fromkeys(labels):
            if not label:
                continue
            first_index = labels.index(label)
            mention_confidence = (
                round(float(scores[first_index]), 6)
                if first_index < len(scores)
                and isinstance(scores[first_index], (int, float))
                else None
            )
            mentions_list.append(
                SemanticMention(
                    "semantic_mentions_entity",
                    f"object:{_slug(label)}",
                    mention_confidence,
                )
            )
        mentions = tuple(mentions_list)
        diagnostics = () if confidence is not None else ("model_confidence_unavailable",)
        return SemanticOutput(
            status="complete",
            confidence=confidence,
            result_json={
                "detections": detections,
                "image_dimensions": {"width": width, "height": height},
                "task": task,
            },
            diagnostics=diagnostics,
            mentions=mentions,
            provenance=vision.runtime,
        )

    return handler


def _place_handler(context: ExtractionContext) -> SemanticOutput:
    gps = (context.derived_metadata or {}).get("gps")
    if not isinstance(gps, dict):
        return SemanticOutput(
            status="complete",
            result_json={"candidates": []},
            diagnostics=("no_reliable_gps_place_candidate",),
            provenance={
                "backend": "canonical-derived-metadata",
                "method": "gps_only_no_reverse_geocoding",
            },
        )
    latitude, longitude = gps.get("latitude"), gps.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return SemanticOutput(
            status="partial",
            result_json={"candidates": []},
            diagnostics=("invalid_gps_place_candidate",),
            provenance={
                "backend": "canonical-derived-metadata",
                "method": "gps_only_no_reverse_geocoding",
            },
        )
    latitude, longitude = float(latitude), float(longitude)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return SemanticOutput(
            status="partial",
            result_json={"candidates": []},
            diagnostics=("invalid_gps_place_candidate",),
            provenance={
                "backend": "canonical-derived-metadata",
                "method": "gps_only_no_reverse_geocoding",
            },
        )
    target = f"geo:{latitude:.6f},{longitude:.6f}"
    return SemanticOutput(
        status="complete",
        confidence=1.0,
        result_json={
            "candidates": [
                {
                    "kind": "gps_coordinate",
                    "latitude": latitude,
                    "longitude": longitude,
                    "confidence": 1.0,
                }
            ]
        },
        mentions=(SemanticMention("semantic_mentions_place", target, 1.0),),
        provenance={
            "backend": "canonical-derived-metadata",
            "method": "gps_only_no_reverse_geocoding",
        },
    )


def build_photo_production_registry(
    backend: PhotoVisionBackend | None = None,
) -> ExtractorRegistry:
    backend = backend or ProductionPhotoBackend()
    registry = ExtractorRegistry()
    definitions = (
        (
            CAPTION_EXTRACTOR,
            PHOTO_EXTRACTOR_VERSION,
            "image_caption",
            _caption_handler(backend),
        ),
        (
            OCR_EXTRACTOR,
            PHOTO_EXTRACTOR_VERSION,
            "ocr_text",
            _ocr_handler(backend),
        ),
        (
            OBJECTS_EXTRACTOR,
            OBJECTS_EXTRACTOR_VERSION,
            "detected_objects",
            _objects_handler(backend),
        ),
        (
            PLACE_EXTRACTOR,
            PHOTO_EXTRACTOR_VERSION,
            "detected_places",
            _place_handler,
        ),
    )
    for name, version, semantic_type, handler in definitions:
        registry.register(
            ExtractorSpec(
                name=name,
                version=version,
                semantic_type=semantic_type,
                object_types=frozenset({"photo"}),
                handler=handler,
            )
        )
    return registry


def run_photo_semantics(
    object_id: str,
    database_path: Path,
    schema_path: Path,
    *,
    backend: PhotoVisionBackend | None = None,
    extractor_names: tuple[str, ...] = PHOTO_EXTRACTORS,
    materialized_path: Path | None = None,
) -> dict[str, SemanticRunResult]:
    """Run the four production PHOTO projections through semantic_results only."""
    extract_media_metadata(
        object_id,
        database_path,
        schema_path,
        materialized_path=materialized_path,
    )
    registry = build_photo_production_registry(backend)
    return {
        name: run_semantic_extractor(
            object_id,
            name,
            database_path,
            schema_path,
            registry=registry,
            materialized_path=materialized_path,
        )
        for name in extractor_names
    }
