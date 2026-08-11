"""Stable identity helpers for Human OS objects.

IDs are assigned before any AI interpretation.  v0.1 uses UUIDv5 so repeated
imports of the same source object produce the same Human OS object ID.
"""

from __future__ import annotations

import uuid

HUMAN_OS_NAMESPACE = uuid.UUID("1e0c237b-834a-4cd4-a713-79c9f50728d5")


def object_id(source: str, source_id: str) -> str:
    """Return a deterministic Human OS ID for a native source object."""
    if not source or not source_id:
        raise ValueError("source and source_id are required")
    value = uuid.uuid5(HUMAN_OS_NAMESPACE, f"object:{source}:{source_id}")
    return f"hos_obj_{value.hex}"


def relation_id(relation_type: str, from_object_id: str, to_object_id: str) -> str:
    """Return a deterministic ID for an edge between two Human OS objects."""
    value = uuid.uuid5(
        HUMAN_OS_NAMESPACE,
        f"relation:{relation_type}:{from_object_id}:{to_object_id}",
    )
    return f"hos_rel_{value.hex}"


def blob_id(content_hash: str) -> str:
    """Return a content-addressed identity that is separate from object identity."""
    if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
        raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
    return f"hos_blob_{content_hash}"


def location_id(object_id_value: str, raw_uri: str) -> str:
    """Return a deterministic identity for one object's RAW location."""
    if not object_id_value or not raw_uri:
        raise ValueError("object_id and raw_uri are required")
    value = uuid.uuid5(
        HUMAN_OS_NAMESPACE,
        f"location:{object_id_value}:{raw_uri}",
    )
    return f"hos_loc_{value.hex}"
