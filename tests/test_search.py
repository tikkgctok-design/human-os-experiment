import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from human_os.search import SearchFilters, search_index
from human_os.search_api import ThreadingHTTPServer, _search, make_handler, serve


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"
SNOW_ID = "hos_obj_11111111111111111111111111111111"
SIGN_ID = "hos_obj_22222222222222222222222222222222"
SUMMER_ID = "hos_obj_33333333333333333333333333333333"


def _semantic(
    conn: sqlite3.Connection,
    object_id: str,
    index: int,
    semantic_type: str,
    *,
    text: str | None = None,
    payload: dict | None = None,
    confidence: float | None = None,
) -> None:
    result_id = f"semantic-{object_id}-{index}"
    conn.execute(
        """
        INSERT INTO semantic_results (
            semantic_result_id, object_id, source_blob_id, source_content_hash,
            extractor_name, extractor_version, semantic_type, status, confidence,
            result_json, result_text, diagnostics_json, provenance_json, is_current,
            created_at
        ) VALUES (?, ?, ?, ?, ?, '1.0.0', ?, 'complete', ?, ?, ?, '[]', ?, 1, ?)
        """,
        (
            result_id, object_id, f"blob-{object_id}", hashlib.sha256(object_id.encode()).hexdigest(),
            f"fixture.{semantic_type}", semantic_type, confidence,
            json.dumps(payload) if payload is not None else None, text,
            json.dumps({"fixture": True}), "2026-08-12T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO semantic_relations (
            semantic_relation_id, object_id, semantic_result_id, relation_type,
            target_ref, confidence, created_at
        ) VALUES (?, ?, ?, 'object_has_semantic_result', ?, ?, ?)
        """,
        (
            f"relation-{result_id}", object_id, result_id, result_id, confidence,
            "2026-08-12T00:00:00+00:00",
        ),
    )
    if semantic_type == "detected_objects":
        for item_index, detection in enumerate((payload or {}).get("detections", [])):
            conn.execute(
                """
                INSERT INTO semantic_relations (
                    semantic_relation_id, object_id, semantic_result_id, relation_type,
                    target_ref, confidence, created_at
                ) VALUES (?, ?, ?, 'semantic_mentions_entity', ?, ?, ?)
                """,
                (
                    f"entity-{result_id}-{item_index}", object_id, result_id,
                    f"object:{detection['label']}", detection.get("confidence"),
                    "2026-08-12T00:00:00+00:00",
                ),
            )


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "index.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        for object_id, occurred_at in (
            (SNOW_ID, "2024-03-23T13:08:58+11:00"),
            (SIGN_ID, "2024-03-26T13:22:58+11:00"),
            (SUMMER_ID, "2023-07-01T12:00:00+11:00"),
        ):
            digest = hashlib.sha256(object_id.encode()).hexdigest()
            conn.execute(
                """
                INSERT INTO objects (
                    object_id, object_type, source, source_id, occurred_at, captured_at,
                    raw_uri, content_hash, mime_type, metadata_json, created_at
                ) VALUES (?, 'photo', 'fixture', ?, ?, ?, ?, ?, 'image/jpeg', '{}', ?)
                """,
                (
                    object_id, object_id, occurred_at, occurred_at,
                    f"file:///archive/{object_id}.jpg", digest,
                    "2026-08-12T00:00:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO blobs VALUES (?, ?, 10, 'image/jpeg', ?)",
                (f"blob-{object_id}", digest, "2026-08-12T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO attachments VALUES (?, ?, ?)",
                (object_id, f"blob-{object_id}", "2026-08-12T00:00:00+00:00"),
            )
        _semantic(conn, SNOW_ID, 1, "image_caption", text="A person standing on the snow.")
        _semantic(
            conn, SNOW_ID, 2, "detected_objects",
            payload={"detections": [{"label": "person", "confidence": 0.99}]},
            confidence=0.99,
        )
        _semantic(conn, SIGN_ID, 1, "image_caption", text="A board with text beside a house.")
        _semantic(conn, SIGN_ID, 2, "ocr_text", text="MOUNTAIN LODGE")
        _semantic(conn, SUMMER_ID, 1, "image_caption", text="A person on a beach.")
    return database


def test_russian_query_ranks_canonical_photo_with_semantic_provenance(tmp_path: Path) -> None:
    database = _database(tmp_path)

    response = search_index("найди фотографии, где человек на снегу", database)

    assert response.results[0].object_id == SNOW_ID
    assert response.results[0].matched_concepts == ("person", "snow")
    assert response.results[0].missing_concepts == ()
    assert response.results[0].raw_uri == f"file:///archive/{SNOW_ID}.jpg"
    assert {item.semantic_type for item in response.results[0].evidence} == {
        "image_caption", "detected_objects"
    }
    assert all(item.source_content_hash for item in response.results[0].evidence)
    assert all(item.provenance == {"fixture": True} for item in response.results[0].evidence)

    snow_only = search_index("найди фотографии со снегом", database)
    assert snow_only.requested_concepts == ("snow",)
    assert snow_only.unsupported_concepts == ()
    assert snow_only.best_effort is False


def test_unknown_swimwear_condition_returns_explained_best_effort(tmp_path: Path) -> None:
    response = search_index(
        "найди фотографии, где человек в плавках на снегу", _database(tmp_path)
    )

    assert response.results[0].object_id == SNOW_ID
    assert response.best_effort is True
    assert response.unsupported_concepts == ("swimwear",)
    assert response.results[0].missing_concepts == ("swimwear",)
    assert "cannot verify" in response.explanation


def test_sign_and_time_media_filters_are_deterministic(tmp_path: Path) -> None:
    database = _database(tmp_path)
    filters = SearchFilters(
        time_from="2024-03-25T00:00:00+11:00",
        time_to="2024-03-27T00:00:00+11:00",
        media_type="photo",
    )

    first = search_index("найди фото с вывеской", database, filters=filters)
    second = search_index("найди фото с вывеской", database, filters=filters)

    assert first.to_dict() == second.to_dict()
    assert [item.object_id for item in first.results] == [SIGN_ID]
    assert first.results[0].matched_concepts == ("signboard",)


def test_search_opens_database_read_only_and_does_not_change_file(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns)

    search_index("снег", database)

    after = (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns)
    assert after == before


@contextmanager
def _running_api(database: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(database))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _post(base_url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{base_url}/v1/search",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def test_v1_health_and_object_endpoints_are_safe(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE semantic_results SET result_json = ? WHERE object_id = ? AND semantic_type = ?",
            (json.dumps({"debug_path": r"G:\RAW\snow.jpg"}), SNOW_ID, "detected_objects"),
        )
        conn.commit()
    with _running_api(database) as base_url:
        with urllib.request.urlopen(f"{base_url}/v1/health", timeout=3) as response:
            health = json.load(response)
        with urllib.request.urlopen(f"{base_url}/v1/object/{SNOW_ID}", timeout=3) as response:
            record = json.load(response)

    assert health == {
        "status": "ok", "read_only": True, "search_version": "1.0.0", "schema_version": 5
    }
    assert record["object_id"] == SNOW_ID
    assert record["safe_ref"] == f"human-os://object/{SNOW_ID}"
    encoded = json.dumps(record)
    assert "raw_uri" not in encoded and "file:///" not in encoded and "\\archive\\" not in encoded
    assert "G:\\RAW" not in encoded and "local-path-redacted" in encoded


def test_v1_search_has_cli_ranking_zero_results_and_no_raw_path_leakage(tmp_path: Path) -> None:
    database = _database(tmp_path)
    query = "найди фотографии, где человек на снегу"
    cli_rank = [item.object_id for item in search_index(query, database).results]
    direct_api = _search({"query": query}, database)
    with _running_api(database) as base_url:
        http_api = _post(base_url, {"query": query})
        empty = _post(base_url, {"query": "uniquevolcanoqxyz"})

    assert [item["object_id"] for item in direct_api["results"]] == cli_rank
    assert [item["object_id"] for item in http_api["results"]] == cli_rank
    assert empty["results"] == []
    assert empty["unsupported_concepts"] == ["uniquevolcanoqxyz"]
    encoded = json.dumps(http_api)
    assert "raw_uri" not in encoded and "file:///" not in encoded and ":\\" not in encoded


def test_v1_swimwear_is_candidate_not_fact(tmp_path: Path) -> None:
    with _running_api(_database(tmp_path)) as base_url:
        payload = _post(base_url, {"query": "найди меня в плавках на снегу"})

    assert payload["best_effort"] is True
    assert payload["unsupported_concepts"] == ["swimwear"]
    assert payload["results"][0]["assessment"] == "candidate"
    assert payload["results"][0]["is_fact"] is False
    assert payload["results"][0]["missing_evidence"] == ["swimwear"]


def test_v1_invalid_and_missing_object_ids(tmp_path: Path) -> None:
    with _running_api(_database(tmp_path)) as base_url:
        with pytest.raises(urllib.error.HTTPError) as invalid:
            urllib.request.urlopen(f"{base_url}/v1/object/not-valid", timeout=3)
        missing_id = "hos_obj_ffffffffffffffffffffffffffffffff"
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base_url}/v1/object/{missing_id}", timeout=3)

    assert invalid.value.code == 400
    assert missing.value.code == 404


def test_v1_repeat_is_idempotent_and_preserves_database_raw_and_semantics(tmp_path: Path) -> None:
    database = _database(tmp_path)
    raw = tmp_path / "snow.jpg"
    raw.write_bytes(b"immutable raw fixture")
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE objects SET raw_uri = ? WHERE object_id = ?", (raw.as_uri(), SNOW_ID))
        conn.commit()
    db_before = (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns)
    raw_before = (raw.read_bytes(), raw.stat().st_size, raw.stat().st_mtime_ns)
    with sqlite3.connect(database) as conn:
        counts_before = (
            conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM semantic_relations").fetchone()[0],
        )

    with _running_api(database) as base_url:
        first = _post(base_url, {"query": "снег"})
        second = _post(base_url, {"query": "снег"})

    with sqlite3.connect(database) as conn:
        counts_after = (
            conn.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM semantic_relations").fetchone()[0],
        )
    assert first == second
    assert counts_after == counts_before
    assert (database.read_bytes(), database.stat().st_size, database.stat().st_mtime_ns) == db_before
    assert (raw.read_bytes(), raw.stat().st_size, raw.stat().st_mtime_ns) == raw_before


def test_v1_retrieval_never_invokes_photo_vision(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("vision must not run during retrieval")

    monkeypatch.setattr("human_os.photo_semantic.run_photo_semantics", forbidden)
    database = _database(tmp_path)
    with _running_api(database) as base_url:
        payload = _post(base_url, {"query": "снег"})

    assert payload["results"][0]["object_id"] == SNOW_ID


def test_server_rejects_non_loopback_binding(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        serve(_database(tmp_path), host="0.0.0.0", port=0)
