"""Research contracts: PIT, security identity, purging, reproducibility and fail-closed release."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from igs.lab import artifacts, dataset, portfolio, readiness, strategies, tracking, training
from igs.pit import PitView
from igs.provenance import validation_fingerprint
from igs.timeutil import end_of_day_ist


def clear_flags():
    return [{"severity": "reject", "status": "clear"}]


def passing():
    values = {}
    for rules in strategies.RULES.values():
        for name, (low, _) in rules.items():
            values[name] = max(low or .1, values.get(name, .1))
    return {name: {'status': 'ok', 'value': value} for name, value in values.items()}


def test_strategy_hypotheses_are_separate_and_missing_is_not_passing():
    evidence = passing()
    assert all(
        r["status"] == "qualified"
        for r in strategies.assess_company(evidence, clear_flags(), "default").values()
    )
    evidence["peg_trailing"]["value"] = 2
    profiles = strategies.assess_company(evidence, clear_flags(), "default")
    assert profiles["garp"]["status"] == "not_qualified"
    assert profiles["compounder"]["status"] == "qualified"
    del evidence["cash_profit_1y"]
    profiles = strategies.assess_company(evidence, clear_flags(), "default")
    assert profiles["inflection"]["status"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "status,expected", [("tripped", "risk_blocked"), ("data_unavailable", "insufficient_evidence")]
)
def test_strategy_governance_blocks(status, expected):
    profiles = strategies.assess_company(
        passing(), [{"severity": "reject", "status": status}], "default"
    )
    assert {p["status"] for p in profiles.values()} == {expected}
    assert {p["status"] for p in strategies.assess_company(passing(), [], "default").values()} == {
        "insufficient_evidence"
    }


def test_unavailable_check_blocks_unless_configured_not_to():
    """As in the production ranking: a reject check whose inputs aren't loaded at all is
    configured `unavailable_blocks: false` and doesn't make governance unknown."""
    flags = [{"severity": "reject", "status": "clear"},
             {"severity": "reject", "status": "data_unavailable", "unavailable_blocks": False}]
    assert {p["status"] for p in strategies.assess_company(passing(), flags, "default")
            .values()} == {"qualified"}
    flags[1]["unavailable_blocks"] = True
    assert {p["status"] for p in strategies.assess_company(passing(), flags, "default")
            .values()} == {"insufficient_evidence"}
    assert strategies.specification()["version"] == 2


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_invalid_readings_and_financial_applicability(value):
    evidence = passing()
    evidence["cash_profit_1y"]["value"] = value
    assert strategies.assess_company(evidence, clear_flags(), "default")["garp"]["status"] == (
        "insufficient_evidence"
    )
    assert strategies.assess_company(passing(), clear_flags(), "bank")["garp"]["status"] == (
        "not_applicable"
    )


@pytest.mark.lookahead
def test_strategy_readings_do_not_change_with_future_facts():
    from test_factors import AS_OF
    from test_research import research_dataset

    data = research_dataset()
    expected = strategies.readings(PitView(data.truncate(AS_OF), AS_OF))
    actual = strategies.readings(PitView(data.poison_future(AS_OF), AS_OF))
    assert actual == expected


def test_acceleration_exact_quarters_and_base_guards(monkeypatch):
    from types import SimpleNamespace

    from igs.factors import base

    quarters = pl.DataFrame(
        {
            "company_id": [1] * 6,
            "qidx": list(range(6)),
            "top_line": [100.0, 100.0, 110.0, 110.0, 120.0, 140.0],
            "pat": [10.0, 10.0, 11.0, 11.0, 12.0, 15.0],
            "ids_top_line": [[n] for n in range(6)],
            "ids_pat": [[n + 10] for n in range(6)],
        }
    )
    monkeypatch.setattr(base, "quarterly", lambda _: quarters)
    monkeypatch.setattr(
        base, "latest_quarter", lambda _: pl.DataFrame({"company_id": [1], "last_q": [5]})
    )
    monkeypatch.setattr(base, "companies", lambda _: pl.DataFrame({"company_id": [1]}))
    assert strategies.acceleration(SimpleNamespace(), "top_line")["value"][0] == pytest.approx(0.2)
    assert strategies.acceleration(SimpleNamespace(), "pat")["value"][0] == pytest.approx(0.3)
    quarters = quarters.filter(pl.col("qidx") != 0)
    assert strategies.acceleration(SimpleNamespace(), "pat")["status"][0] == "insufficient_data"


@pytest.mark.lookahead
def test_labels_use_next_exact_session_same_security_and_no_stale_exit():
    days = [dt.date(2020, 1, n) for n in (2, 3, 6, 7)]
    features = pl.DataFrame(
        {"as_of": [end_of_day_ist(days[0])] * 2, "company_id": [1, 2], "security_id": [10, 20]}
    )
    prices = pl.DataFrame(
        {
            "company_id": [1, 1, 1, 2, 2],
            "security_id": [10, 10, 11, 20, 20],
            "trade_date": [days[1], days[2], days[3], days[1], days[3]],
            "tr": [100.0, 120.0, 500.0, 200.0, 220.0],
        }
    )
    labels = dataset.exact_labels(features, prices, days, 2)
    assert labels["target"].to_list() == [None, pytest.approx(0.1)]
    assert labels["entry_date"].to_list() == [days[1]] * 2
    assert labels["label_end"].to_list() == [days[3]] * 2
    assert labels["label_available_at"][1] > features["as_of"][1]
    with pytest.raises(ValueError, match="duplicate"):
        dataset.exact_labels(features, pl.concat([prices, prices.head(1)]), days, 2)
    with pytest.raises(ValueError, match="horizon"):
        dataset.exact_labels(features, prices, days, 0)


@pytest.mark.lookahead
def test_purging_removes_boundary_overlaps_and_keeps_whole_dates():
    asofs = [
        dt.date(2019, 1, 1),
        dt.date(2019, 12, 30),
        dt.date(2020, 1, 2),
        dt.date(2020, 12, 30),
        dt.date(2021, 1, 2),
    ]
    ends = [d + dt.timedelta(days=10) for d in asofs]
    frame = pl.DataFrame(
        {
            "as_of": [end_of_day_ist(d) for d in asofs],
            "label_available_at": [end_of_day_ist(d) for d in ends],
            "target": [0.1] * 5,
        }
    )
    train, val, test = training.purged_split(frame, dt.date(2020, 1, 1), dt.date(2021, 1, 1))
    assert [p.height for p in (train, val, test)] == [1, 1, 1]
    assert train["as_of"][0] == frame["as_of"][0]
    assert val["as_of"][0] == frame["as_of"][2]
    assert test["as_of"][0] == frame["as_of"][4]


def test_preprocessing_is_train_only_and_marks_missing():
    frame = pl.DataFrame({"x": [1.0, None, 3.0]})
    pre = training.fit_transform(frame, ["x"])
    assert pre["medians"] == [2.0]
    transformed = training.transform(pl.DataFrame({"x": [None, 100.0]}), pre)
    assert transformed[0, 0] == 0
    assert transformed[0, 1] == pytest.approx(2 / 3)
    assert pre["means"] == [2.0]
    assert training.drift(pl.DataFrame({"x": [100.0, 101.0]}), pre)["status"] == "REVIEW"
    with pytest.raises(ValueError, match="no observations"):
        training.fit_transform(pl.DataFrame({"x": [None, None]}, schema={"x": pl.Float64}), ["x"])


def test_bundles_atomic_no_overwrite_and_integrity(tmp_path):
    output = tmp_path / "bundle"
    with pytest.raises(RuntimeError):
        with artifacts.bundle(output) as path:
            artifacts.write_json(path / "data.json", {"a": 1})
            raise RuntimeError("Interrupted")
    assert not output.exists()
    with artifacts.bundle(output) as path:
        artifacts.write_json(path / "data.json", {"a": 1})
        artifacts.seal(path, {"kind": "test"})
    assert artifacts.verify(output)["kind"] == "test"
    with pytest.raises(FileExistsError):
        with artifacts.bundle(output):
            pass
    (output / "data.json").write_text("changed")
    with pytest.raises(ValueError, match="checksum"):
        artifacts.verify(output)


@pytest.fixture
def feature_bundle(tmp_path):
    """Synthetic identity/learning fixture. Never used as investment evidence."""
    source = tmp_path / "features"
    rows, labels = [], []
    for year in range(2017, 2021):
        for month in range(1, 13):
            asof = end_of_day_ist(dt.date(year, month, 1))
            for cid in range(10):
                key = {"as_of": asof, "company_id": cid + 1, "security_id": cid + 101}
                rows.append({**key, "x": float(cid), "z": float(month)})
                labels.append(
                    {
                        **key,
                        "entry_date": asof.date() + dt.timedelta(days=1),
                        "label_end": asof.date() + dt.timedelta(days=10),
                        "label_available_at": asof + dt.timedelta(days=10),
                        "target": cid * 0.01,
                    }
                )
    with artifacts.bundle(source) as path:
        pl.DataFrame(rows).write_parquet(path / "features.parquet")
        pl.DataFrame(labels).write_parquet(path / "labels.parquet")
        artifacts.seal(
            path,
            {
                "kind": "igs-feature-dataset",
                "features": ["x", "z"],
                "code_hash": validation_fingerprint(),
                "horizon_sessions": 5,
                "frequency": "monthly",
            },
        )
    return source


@pytest.mark.parametrize("algorithm", ["ridge", "lightgbm"])
def test_train_predict_reproducible_and_paper_only(
    feature_bundle, tmp_path, monkeypatch, algorithm
):
    if algorithm == "lightgbm":
        pytest.importorskip("lightgbm")
    monkeypatch.setattr("igs.pit.gate.require_gate", lambda: None)
    outputs = [tmp_path / "run1", tmp_path / "run2"]
    for output in outputs:
        manifest = training.train(
            feature_bundle, output, dt.date(2019, 1, 1), dt.date(2020, 1, 1), algorithm=algorithm
        )
        assert manifest["paper_only"] and manifest["stage"] == "research"
        assert manifest["split"]["train_rows"] == 240
        assert manifest["split"]["holdout_rows"] == 120
    first = pl.read_parquet(outputs[0] / "predictions.parquet")
    assert_frame_equal(first, pl.read_parquet(outputs[1] / "predictions.parquet"))
    snapshot = tmp_path / "snapshot"
    original = artifacts.verify(feature_bundle)
    with artifacts.bundle(snapshot) as path:
        pl.read_parquet(feature_bundle / "features.parquet").filter(
            pl.col("as_of") >= end_of_day_ist(dt.date(2020, 1, 1))
        ).write_parquet(path / "features.parquet")
        artifacts.seal(path, original)
    prediction = tmp_path / "paper"
    result = training.predict_snapshot(outputs[0], snapshot, prediction)
    assert result["paper_only"]
    actual = pl.read_parquet(prediction / "predictions.parquet")
    expected = first.filter(pl.col("split") == "holdout").select(*dataset.KEYS, "prediction")
    assert_frame_equal(actual, expected)
    with pytest.raises(ValueError, match="latest training label"):
        training.predict_snapshot(outputs[0], feature_bundle, tmp_path / "bad-predictions")


def test_feature_export_real_pipeline_on_synthetic_market(tmp_path, monkeypatch):
    import synthetic_market as market
    from test_backtest import _cfgs

    monkeypatch.setattr(dataset, "require_gate", lambda: None)
    _, sc, uc, _ = _cfgs()
    monkeypatch.setattr(dataset, "load_scoring", lambda: sc)
    monkeypatch.setattr(dataset, "load_universe", lambda: uc)
    output = tmp_path / "snapshot"
    result = dataset.export(
        market.build(), dt.date(2021, 1, 1), dt.date(2021, 1, 31), output, horizon=5
    )
    assert result["rows"] > 0 and result["labelled_rows"] > 0
    features = pl.read_parquet(output / "features.parquet")
    assert "target" not in features.columns
    assert set(dataset.FEATURES) <= set(features.columns)
    assert artifacts.verify(output)["schema_version"] == 1


def test_no_evidence_never_reports_ready(tmp_path):
    result = readiness.review(tmp_path / "ready.json")
    assert result["status"] == "BLOCKED" and not result["live_trading_enabled"]
    assert all(c["status"] == "BLOCKED" for c in result["checks"])


def test_paper_portfolio_has_independent_concentration_and_risk_limits():
    frame = pl.DataFrame({'company_id': list(range(30)), 'prediction': list(range(30)),
        'risk_clear': [True]*30, 'adv': [1e8]*30,
        'industry': ['industry'+str(i % 5) for i in range(30)]})
    picks = portfolio.select(frame, 10, .2)
    assert picks.height == 10
    assert picks.group_by('industry').len()['len'].max() == 2
    blocked = frame.with_columns(pl.lit(False).alias('risk_clear'))
    assert portfolio.select(blocked, 10, .2).is_empty()
    # Too few names, or one industry: hold what the limits allow at 1/size, the rest cash.
    concentrated = frame.with_columns(pl.lit('one industry').alias('industry'))
    picks = portfolio.select(concentrated, 10, .2)
    assert picks.height == 2 and picks['weight'].to_list() == [.1, .1]
    picks = portfolio.select(frame.head(9), 10, .2)
    assert picks.height == 9 and picks['weight'].sum() == pytest.approx(.9)


def test_paper_portfolio_keeps_holdings_within_the_buffer():
    """A held name stays while it ranks within the best 2 x size; beyond that it is
    replaced by the best-ranked names."""
    frame = pl.DataFrame({'company_id': list(range(40)), 'prediction': [40 - i for i in range(40)],
        'risk_clear': [True]*40, 'adv': [1e8]*40,
        'industry': ['industry'+str(i % 10) for i in range(40)]})
    fresh = portfolio.select(frame, 10, .2)
    assert fresh['company_id'].to_list() == list(range(10))
    kept = portfolio.select(frame, 10, .2, held={15, 25})       # ranks 16 and 26
    assert 15 in kept['company_id'].to_list() and 25 not in kept['company_id'].to_list()
    assert kept.height == 10 and 9 not in kept['company_id'].to_list()
    assert portfolio.select(frame, 10, .2, held={15}, buffer=1)['company_id'].to_list() == (
        list(range(10)))


def test_model_portfolio_retains_missing_prices_and_cash(tmp_path, monkeypatch):
    monkeypatch.setattr('igs.pit.gate.require_gate', lambda: None)
    signals = [dt.date(2020, 1, 31), dt.date(2020, 2, 28), dt.date(2020, 3, 31)]
    executions = [dt.date(2020, 2, 3), dt.date(2020, 3, 2), dt.date(2020, 4, 1)]
    meta, forecasts, prices = [], [], []
    for i, (day, execution) in enumerate(zip(signals, executions, strict=True)):
        for cid in range(10):
            key = {'as_of': end_of_day_ist(day).astimezone(dt.UTC),
                   'company_id': cid, 'security_id': cid+100}
            meta.append({**key, 'risk_clear': True, 'industry': str(cid % 5),
                         'adv': 1e8, 'vol': .02, 'bucket': 'mid'})
            forecasts.append({**key, 'prediction': float(cid)})
            # Company 0 lacks its true second execution price. It must remain selected.
            if not (cid == 0 and i == 1):
                prices.append({'company_id': cid, 'security_id': cid+100,
                               'trade_date': execution, 'tr': 100.+i*10})
    source, prediction, result = tmp_path/'features', tmp_path/'predictions', tmp_path/'portfolio'
    with artifacts.bundle(source) as path:
        pl.DataFrame(meta).write_parquet(path/'execution.parquet')
        pl.DataFrame(prices).write_parquet(path/'prices.parquet')
        pl.DataFrame({'trade_date': executions, 'bench': [100., 110., 120.]}).write_parquet(
            path/'benchmark.parquet')
        artifacts.write_json(path/'coverage.json', [{'date': str(d)} for d in signals])
        artifacts.write_json(path/'calendar.json', [str(d) for d in sorted(signals+executions)])
        artifacts.seal(path, {'kind': 'igs-feature-dataset', 'frequency': 'monthly',
                             'benchmark_name': 'test TRI'})
    with artifacts.bundle(prediction) as path:
        pl.DataFrame(forecasts).write_parquet(path/'predictions.parquet')
        artifacts.seal(path, {'kind': 'igs-paper-predictions',
                             'dataset_manifest_hash': artifacts.digest(source/'manifest.json')})
    portfolio.backtest(source, prediction, result, size=10, industry_cap=.2)
    summary = json.loads((result/'summary.json').read_text())
    assert summary['configured_costs']['status'] == 'INCONCLUSIVE'
    assert summary['configured_costs']['missing_exit_prices'] == 1
    selected = pl.read_parquet(result/'selected.parquet')
    assert selected.height == 30
    # Proves the missing future exit did not remove a selected security.
    assert selected.filter(pl.col('company_id') == 0).height == 3


def test_readiness_rejects_old_comparison(tmp_path):
    comparison = tmp_path / "comparison.json"
    artifacts.write_json(comparison, {"fingerprint": "old", "variants": {}})
    result = readiness.review(tmp_path / "ready.json", comparison=comparison)
    assert result["checks"][0]["status"] == "BLOCKED"


def test_tracking_explicit_https_and_sanitized_metrics(tmp_path):
    model = tmp_path / "model"
    with artifacts.bundle(model) as path:
        artifacts.write_json(path / "metrics.json", {"holdout": {"mean_date_ic": 0.05}})
        artifacts.seal(path, {"kind": "igs-model-experiment", "parameters": {"seed": 1729}})
    with pytest.raises(ValueError, match="HTTPS"):
        tracking.publish(model, "http://example.com", "1")
    requests = []

    def handle(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"run": {"info": {"run_id": "test-run"}}})

    assert (
        tracking.publish(
            model, "https://mlflow.example.test", "1", transport=httpx.MockTransport(handle)
        )
        == "test-run"
    )
    assert len(requests) == 3
    assert requests[1][1]["metrics"][0]["key"] == "holdout.mean_date_ic"
    assert requests[2][1]["status"] == "FINISHED"


def test_research_cli_readiness_does_not_connect_database(tmp_path):
    from igs.cli import build_parser

    args = build_parser().parse_args(
        ["research", "readiness", "--output", str(tmp_path / "r.json")]
    )
    assert args.fn(args) == 2
    assert json.loads((tmp_path / "r.json").read_text())["status"] == "BLOCKED"


@pytest.mark.db
def test_operations_reads_migrated_database_without_writes(db_conn, tmp_path):
    from igs.research import audit
    coverage = audit(db_conn, tmp_path/'coverage.json')
    assert coverage['prices'] == [] and coverage['benchmarks'] == []
    result = readiness.operations(db_conn)
    assert result["unresolved_orders"] == 0
    assert not result["broker_positions_verified"]
    assert not db_conn.execute("select enabled from intraday_trading_settings").fetchone()[0]
