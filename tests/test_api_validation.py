"""API failure paths, including installations without the optional assistant SDK."""
import sys

from fastapi.testclient import TestClient

from igs.api.app import app, get_conn


def test_missing_assistant_dependency_returns_503(monkeypatch):
    monkeypatch.setitem(sys.modules, "igs.assistant.llm", None)
    app.dependency_overrides[get_conn] = lambda: object()
    try:
        with TestClient(app) as client:
            assert client.post("/ask", json={"question": "Explain this run"}).status_code == 503
            assert client.get("/stocks/TEST/brief").status_code == 503
            assert client.post("/ask", json={"question": "test", "history": [
                {"role": "user"}]}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_screen_rejects_invalid_filter_values():
    app.dependency_overrides[get_conn] = lambda: object()
    try:
        with TestClient(app) as client:
            for filters in ({"min_score": "abc"}, {"watchlist_only": "false"}, {"tier": []}):
                assert client.post("/screens", json={"name": "bad", "filters": filters}
                                   ).status_code == 422
    finally:
        app.dependency_overrides.clear()
