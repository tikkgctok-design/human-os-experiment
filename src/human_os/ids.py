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
