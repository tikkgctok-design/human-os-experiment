import sqlite3
from pathlib import Path

from PIL import Image

from human_os.photo_import import import_folder, import_photo, main
from human_os.photo_semantic import PhotoVisionResult

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "human_os.sqlite.sql"


class FixtureVisionBackend:
    """Deterministic fake backend so tests never invoke Florence-2/DETR."""

    def __init__(self, *, fail_for: frozenset[str] = frozenset()) -> None:
        self.fail_for = fail_for
        self.calls: list[tuple[Path, str]] = []

    def analyze(self, path: Path, task: str) -> PhotoVisionResult:
        self.calls.append((path, task))
        if path.name in self.fail_for:
            raise RuntimeError(f"synthetic backend failure for {path.name}")
        parsed = {
            "<DETAILED_CAPTION>": "A test photo.",
            "<OCR_WITH_REGION>": {"labels": [], "quad_boxes": [], "scores": []},
            "<OD>": {"labels": [], "bboxes": [], "scores": []},
        }[task]
        return PhotoVisionResult(
            task=task,
            parsed=parsed,
            runtime={"backend": "fixture", "model_id": "fixture/model", "model_revision": "fixture-sha"},
        )


def _make_jpeg(path: Path, color: tuple[int, int, int] = (10, 20, 30)) -> None:
    Image.new("RGB", (16, 12), color=color).save(path, format="JPEG")


def _counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("objects", "semantic_results")
        }


def test_empty_folder_returns_no_outcomes(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    db = tmp_path / "index.sqlite"

    outcomes = import_folder(folder, db, SCHEMA, backend=FixtureVisionBackend())

    assert outcomes == []


def test_non_jpeg_files_are_ignored(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "a.jpg")
    (folder / "notes.txt").write_text("not a photo")
    (folder / "picture.png").write_bytes(b"\x89PNG\r\n")
    (folder / "sub").mkdir()
    _make_jpeg(folder / "sub" / "nested.jpg")

    outcomes = import_folder(folder, tmp_path / "index.sqlite", SCHEMA, backend=FixtureVisionBackend())

    assert [outcome.filename for outcome in outcomes] == ["a.jpg"]


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "one.JPG")
    _make_jpeg(folder / "two.Jpeg")
    _make_jpeg(folder / "three.jpg")

    outcomes = import_folder(folder, tmp_path / "index.sqlite", SCHEMA, backend=FixtureVisionBackend())

    assert sorted(outcome.filename for outcome in outcomes) == ["one.JPG", "three.jpg", "two.Jpeg"]


def test_successful_import_writes_object_and_semantic_results(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "a.jpg")
    db = tmp_path / "index.sqlite"
    original_bytes = (folder / "a.jpg").read_bytes()

    outcomes = import_folder(folder, db, SCHEMA, backend=FixtureVisionBackend())

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.filename == "a.jpg"
    assert outcome.object_id and outcome.object_id.startswith("hos_obj_")
    assert outcome.ingestion_outcome == "inserted"
    assert outcome.semantic_status == "complete"
    assert outcome.error is None
    assert (folder / "a.jpg").read_bytes() == original_bytes
    counts = _counts(db)
    assert counts["objects"] == 1
    assert counts["semantic_results"] == 4


def test_repeat_run_is_idempotent(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "a.jpg")
    _make_jpeg(folder / "b.jpg", color=(200, 100, 50))
    db = tmp_path / "index.sqlite"
    backend = FixtureVisionBackend()

    first = import_folder(folder, db, SCHEMA, backend=backend)
    second = import_folder(folder, db, SCHEMA, backend=backend)

    assert {outcome.ingestion_outcome for outcome in first} == {"inserted"}
    assert {outcome.ingestion_outcome for outcome in second} == {"unchanged"}
    assert [outcome.object_id for outcome in first] == [outcome.object_id for outcome in second]
    counts = _counts(db)
    assert counts["objects"] == 2
    assert counts["semantic_results"] == 8


def test_one_file_error_does_not_stop_the_rest(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "bad.jpg")
    _make_jpeg(folder / "good.jpg")
    backend = FixtureVisionBackend(fail_for=frozenset({"bad.jpg"}))

    outcomes = import_folder(folder, tmp_path / "index.sqlite", SCHEMA, backend=backend)

    # The existing semantic pipeline already converts backend exceptions into a
    # durable "failed" semantic_results row instead of raising (see
    # semantic_extraction.run_semantic_extractor), so ingestion still succeeds and
    # only the semantic status reflects the failure.
    by_name = {outcome.filename: outcome for outcome in outcomes}
    assert by_name["bad.jpg"].error is None
    assert by_name["bad.jpg"].object_id is not None
    assert by_name["bad.jpg"].semantic_status == "failed"
    assert by_name["good.jpg"].error is None
    assert by_name["good.jpg"].semantic_status == "complete"


def test_run_photo_semantics_exception_is_captured_as_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A genuinely unhandled exception from run_photo_semantics (e.g. a broken DB
    connection) must still be caught by import_photo, not crash the batch."""
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "a.jpg")
    db = tmp_path / "index.sqlite"

    def raise_boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("human_os.photo_import.run_photo_semantics", raise_boom)

    outcome = import_photo(folder / "a.jpg", db, SCHEMA, backend=FixtureVisionBackend())

    assert outcome.filename == "a.jpg"
    assert outcome.ingestion_outcome == "inserted"
    assert outcome.object_id is not None
    assert outcome.error is not None and "boom" in outcome.error
    assert outcome.semantic_status is None


def test_cli_missing_directory_exits_with_code_2(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "does-not-exist"

    exit_code = main([str(missing), "--db", str(tmp_path / "index.sqlite"), "--schema", str(SCHEMA)])

    assert exit_code == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_reports_totals_and_exit_code_for_mixed_results(tmp_path: Path, capsys, monkeypatch) -> None:
    """The CLI never picks a backend itself, so a Florence/DETR call here would be
    a bug; monkeypatching run_photo_semantics keeps this test model-free while still
    exercising main()'s real per-file loop, print format and exit code."""
    folder = tmp_path / "photos"
    folder.mkdir()
    _make_jpeg(folder / "bad.jpg")
    _make_jpeg(folder / "good.jpg")
    calls = {"count": 0}

    def fake_run_photo_semantics(object_id, database_path, schema_path, *, backend=None, extractor_names=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr("human_os.photo_import.run_photo_semantics", fake_run_photo_semantics)

    exit_code = main([
        str(folder), "--db", str(tmp_path / "index.sqlite"), "--schema", str(SCHEMA),
    ])

    captured = capsys.readouterr()
    assert "total=2 succeeded=1 failed=1" in captured.out
    assert exit_code == 1


def test_cli_empty_folder_exits_zero(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()

    exit_code = main([str(folder), "--db", str(tmp_path / "index.sqlite"), "--schema", str(SCHEMA)])

    assert exit_code == 0
    assert "total=0 succeeded=0 failed=0" in capsys.readouterr().out
