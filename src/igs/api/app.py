"""HTTP API (read-mostly). Run with `igs api` or `uvicorn igs.api.app:app`.

Every response carries the disclaimer, in the body and in an X-Disclaimer
header. The only writes are the user's own watchlist and saved screens; there
is no endpoint that places or routes orders.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from igs import service
from igs.db import connect
from igs.guardrails import DISCLAIMER

app = FastAPI(title="IndiaGrowthScreener", description=DISCLAIMER, version="0.1.0")


def get_conn() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.middleware("http")
async def disclaimer_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Disclaimer"] = "Personal research tool. Not investment advice."
    return response


@app.exception_handler(service.NotFound)
async def not_found(request: Request, exc: service.NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "disclaimer": DISCLAIMER})


def _wrap(**payload) -> dict:
    return {"disclaimer": DISCLAIMER, **payload}


@app.get("/health")
def health() -> dict:
    return _wrap(status="ok")


@app.get("/disclaimer")
def disclaimer() -> dict:
    return _wrap()


@app.get("/runs")
def list_runs(conn=Depends(get_conn)) -> dict:
    return _wrap(runs=service.runs(conn))


@app.get("/rankings")
def rankings(conn=Depends(get_conn), run_id: int | None = None, tier: str | None = None,
             sector: str | None = None, industry: str | None = None,
             bucket: str | None = None, q: str | None = None, min_score: float | None = None,
             watchlist_only: bool = False, limit: int | None = Query(None, ge=1, le=5000)
             ) -> dict:
    run, rows = service.rankings(conn, run_id, tier, sector, industry, bucket, q, min_score,
                                 watchlist_only, limit)
    return _wrap(run=run, count=len(rows), rows=rows)


@app.get("/rankings.csv", response_class=PlainTextResponse)
def rankings_csv(conn=Depends(get_conn), run_id: int | None = None, tier: str | None = None,
                 sector: str | None = None, industry: str | None = None,
                 bucket: str | None = None, q: str | None = None) -> PlainTextResponse:
    _, rows = service.rankings(conn, run_id, tier, sector, industry, bucket, q)
    return PlainTextResponse(service.rankings_csv(rows), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=rankings.csv"})


@app.get("/facets")
def facets(conn=Depends(get_conn), run_id: int | None = None) -> dict:
    return _wrap(facets=service.facets(conn, run_id))


@app.get("/stocks/{symbol}")
def stock(symbol: str, conn=Depends(get_conn), run_id: int | None = None) -> dict:
    return service.stock_detail(conn, symbol, run_id)


@app.get("/stocks/{symbol}/why")
def why(symbol: str, conn=Depends(get_conn), run_id: int | None = None) -> dict:
    d = service.stock_detail(conn, symbol, run_id)
    return _wrap(symbol=symbol, run=d["run"], text=d["company"]["explanation"],
                 top_contributions=d["top_contributions"], red_flags=d["red_flags"],
                 cautions=d["cautions"], hc_blockers=d["hc_blockers"],
                 robustness=d["robustness"], run_health_issues=d["run"]["health_issues"])


class WatchItem(BaseModel):
    symbol: str
    note: str = ""


@app.get("/watchlist")
def get_watchlist(conn=Depends(get_conn)) -> dict:
    return _wrap(items=service.watchlist(conn))


@app.post("/watchlist")
def add_watchlist(item: WatchItem, conn=Depends(get_conn)) -> dict:
    return _wrap(added=service.watchlist_add(conn, item.symbol, item.note))


@app.delete("/watchlist/{symbol}")
def remove_watchlist(symbol: str, conn=Depends(get_conn)) -> dict:
    service.watchlist_remove(conn, symbol)
    return _wrap(removed=symbol)


class Screen(BaseModel):
    name: str
    filters: dict


@app.get("/screens")
def get_screens(conn=Depends(get_conn)) -> dict:
    return _wrap(screens=service.screens(conn))


@app.post("/screens")
def save_screen(screen: Screen, conn=Depends(get_conn)) -> dict:
    try:
        service.screen_save(conn, screen.name, screen.filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _wrap(saved=screen.name)


@app.delete("/screens/{name}")
def delete_screen(name: str, conn=Depends(get_conn)) -> dict:
    service.screen_delete(conn, name)
    return _wrap(deleted=name)


@app.get("/screens/{name}/results")
def run_screen(name: str, conn=Depends(get_conn), run_id: int | None = None) -> dict:
    try:
        run, rows = service.screen_run(conn, name, run_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _wrap(run=run, screen=name, count=len(rows), rows=rows)


# --------------------------------------------------------------------------- assistant
# The optional research assistant (config/assistant.yaml). Its answers are AI-written from
# a run's stored results; they are not used in ranking. Each call costs API credits, which
# is one more reason the API listens on localhost by default.


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    run_id: int | None = None
    history: list[HistoryTurn] = Field(default_factory=list, max_length=20)


def _assistant(conn):
    try:
        from igs.assistant.llm import Assistant
    except ImportError as exc:
        raise HTTPException(503, "the assistant needs the Anthropic SDK: "
                                 "run `uv sync --all-groups`") from exc
    return Assistant.open(conn)


def _assistant_errors():
    try:
        from igs.assistant.llm import AssistantError, AssistantUnavailable
    except ImportError as exc:
        raise HTTPException(503, "the assistant needs the Anthropic SDK: "
                                 "run `uv sync --all-groups`") from exc
    return AssistantUnavailable, AssistantError


@app.post("/ask")
def ask_question(q: Question, conn=Depends(get_conn)) -> dict:
    unavailable, failed = _assistant_errors()
    from igs.assistant.ask import ask
    try:
        a = ask(_assistant(conn), q.question, q.run_id,
                [h.model_dump() for h in q.history])
    except unavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except failed as exc:
        raise HTTPException(502, str(exc)) from exc
    return _wrap(answer=a.text, ai_generated=True, run_id=a.run_id, as_of=a.as_of,
                 lookups=a.tool_calls, cost_usd=round(a.cost_usd, 4), model=a.model,
                 notes=a.notes)


@app.get("/stocks/{symbol}/brief")
def stock_brief(symbol: str, conn=Depends(get_conn), run_id: int | None = None,
                refresh: bool = False) -> dict:
    unavailable, failed = _assistant_errors()
    from igs.assistant.brief import brief
    try:
        b = brief(_assistant(conn), symbol, run_id, refresh=refresh)
    except unavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except failed as exc:
        raise HTTPException(502, str(exc)) from exc
    return _wrap(brief=b.text, ai_generated=True, symbol=b.symbol, run_id=b.run_id,
                 model=b.model, stored=b.cached, cost_usd=round(b.cost_usd, 4))
