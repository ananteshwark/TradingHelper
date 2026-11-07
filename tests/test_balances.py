"""What is left on the AI providers' accounts (igs.assistant.balances)."""
import datetime as dt

import httpx
import pytest

from igs.assistant import balances


def _client(routes: dict[str, tuple[int, dict]], seen: list | None = None) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((str(request.url), request.headers.get("authorization")))
        status, body = routes[request.url.path]
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_deepseek_balance(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    seen = []
    client = _client({"/user/balance": (200, {"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": "12.50", "granted_balance": "2.50",
         "topped_up_balance": "10.00"}]})}, seen)
    assert balances.deepseek(client) == {"available": True, "balances": [
        {"currency": "USD", "total": 12.5, "granted": 2.5, "topped_up": 10.0}]}
    assert seen == [("https://api.deepseek.com/user/balance", "Bearer sk-test")]


def test_openrouter_account_credits_with_a_management_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-mgmt")
    client = _client({"/api/v1/credits": (200, {"data": {"total_credits": 100.5,
                                                         "total_usage": 25.75}})})
    assert balances.openrouter(client) == {"scope": "account", "credits": 100.5,
                                           "used": 25.75, "left": 74.75}


def test_openrouter_falls_back_to_the_keys_own_limit(monkeypatch):
    """/credits answers only a management key; a normal key gets 403 and its own limit."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-normal")
    routes = {"/api/v1/credits": (403, {"error": {"code": 403, "message": "management"}}),
              "/api/v1/key": (200, {"data": {"limit": 20.0, "limit_remaining": 7.5,
                                             "usage": 12.5}})}
    assert balances.openrouter(_client(routes)) == {"scope": "key", "limit": 20.0,
                                                    "used": 12.5, "left": 7.5}
    routes["/api/v1/key"] = (200, {"data": {"limit": None, "limit_remaining": None,
                                            "usage": 3.0}})
    assert balances.openrouter(_client(routes))["left"] is None


def test_only_providers_with_a_key_are_read_and_failures_say_why(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert balances.live(_client({})) == {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bad")
    out = balances.live(_client({"/api/v1/credits": (401, {"error": {"code": 401}})}))
    assert list(out) == ["openrouter"] and "HTTP 401" in out["openrouter"]


@pytest.mark.db
def test_the_anthropic_balance_is_estimated_from_the_spend_logged_since(db_conn,
                                                                     monkeypatch):
    c = db_conn
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert balances.estimate(c) is None
    assert "no balance entered" in balances.lines(c)[0]
    with pytest.raises(ValueError):
        balances.record(c, -1)

    def spend(cost: float, provider: str = "anthropic", when: str = "now()") -> None:
        c.execute(f"""insert into llm_call (feature, model, input_tokens, output_tokens,
                          cost_usd, provider, called_at)
                      values ('call', 'm', 1, 1, %s, %s, {when})""", (cost, provider))

    spend(5.0, when="now() - interval '1 day'")      # before the balance was entered
    balances.record(c, 40)
    spend(1.25)
    spend(0.5)
    spend(9.0, provider="deepseek")                   # another provider's spend
    c.commit()
    est = balances.estimate(c)
    assert (est["entered"], est["spent"], est["left"]) == (40.0, 1.75, 38.25)
    assert est["as_of"] <= dt.datetime.now(dt.UTC)
    assert balances.lines(c)[0].startswith("Anthropic: about $38.25 left ($40.00 entered")
    balances.record(c, 100)                           # a top-up: the latest entry counts
    assert balances.estimate(c)["left"] == 100.0
