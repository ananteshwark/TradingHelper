from pathlib import Path

from igs.storage import inventory


def test_ranking_counts_allocated_space_and_ignores_links(tmp_path):
    for i in range(1, 5):
        folder = tmp_path / str(i)
        folder.mkdir()
        (folder / "file").write_bytes(b"x" * (i * 8192))
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "alias").hardlink_to(tmp_path / "4" / "file")
    result = inventory(tmp_path)
    assert [size for size, _ in result["files"]] == [32768, 24576, 16384]
    assert len(result["folders"]) == 3
    assert not result["timed_out"]
    assert result["skipped"] == 0
    assert result["free"] > 0


def test_deadline_reports_partial_results(tmp_path):
    (tmp_path / "file").write_text("data")
    result = inventory(tmp_path, seconds=0)
    assert result["timed_out"]
    assert result["files"] == []


def test_inaccessible_folder_is_reported(tmp_path, monkeypatch):
    from igs import storage
    (tmp_path / "protected").mkdir()
    original = storage.os.scandir

    def scan(path):
        if Path(path).name == "protected":
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(storage.os, "scandir", scan)
    assert inventory(tmp_path)["skipped"] == 1


def test_storage_page_renders_metrics_and_partial_warning(monkeypatch):
    from streamlit.testing.v1 import AppTest

    from igs.timeutil import utc_now
    from igs.ui import storage
    monkeypatch.setattr(storage, "_inventory", lambda: ({
        "files": [(1024, "/example/file")], "folders": [(2048, "/example")],
        "skipped": 1, "timed_out": False}, utc_now()))
    app = AppTest.from_string("from igs.ui.storage import render\nrender()").run()
    assert not app.exception
    assert [m.label for m in app.metric] == ["Available", "Used", "Total"]
    assert len(app.dataframe) == 2
    assert "Partial ranking" in app.warning[0].value
