import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.contracts import ExecutionRequest, ToolRequest
from app.evaluator import evaluate
from app.fixture import create_fixture
from app.gateway import ToolGateway
from app.main import create_app
from app.manager import Manager
from app.providers import LiteLLMProvider, MockProvider, Reply, registry
from app.store import Store


@pytest.fixture(autouse=True)
def legacy_model_defaults(monkeypatch):
    monkeypatch.setenv("MODEL_A", "openai/gpt-4o-mini")
    monkeypatch.setenv("MODEL_B", "anthropic/claude-haiku-4-5")


@asynccontextmanager
async def client_for(path, **kwargs):
    app = create_app(path, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
            yield client, app


async def launch(client, payload=None):
    payload = {"agents": [{"model": "mock/explorer-a"}, {"model": "mock/explorer-b"}], **(payload or {})}
    response = await client.post("/executions", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["execution_id"]


async def done(client, execution_id):
    for _ in range(400):
        data = (await client.get(f"/executions/{execution_id}")).json()
        if data["status"] not in {"pending", "running"}:
            return data
        await asyncio.sleep(0.005)
    raise AssertionError("execution did not finish")


@pytest.mark.parametrize("version,vulnerabilities", [("vulnerable", 1), ("fixed", 0)])
def test_end_to_end(tmp_path, version, vulnerabilities):
    async def run():
        async with client_for(tmp_path) as (client, app):
            assert (await client.get("/health")).status_code == 200
            assert len((await client.get("/models")).json()) == 6
            assert (await client.get("/scenarios")).json()[0]["id"] == "orders-v1"
            execution_id = await launch(client, {"environment": version})
            result = await done(client, execution_id)
            assert result["status"] == "completed"
            assert all(a["status"] == "completed" for a in result["agents"])
            summary = (await client.get(f"/executions/{execution_id}/report")).json()
            assert summary["unique_vulnerability_count"] == vulnerabilities
            assert summary["comparison"]["complete"] is True
            assert len(summary["comparison"]["common"]) == vulnerabilities
            assert summary["methods"]["cross_account"] == {
                "attempts": 4, "reproduced": 4 if vulnerabilities else 0,
                "not_reproduced": 0 if vulnerabilities else 4, "errors": 0,
                "inconclusive_or_baseline": 0,
                "reproduction_success_rate": 1 if vulnerabilities else 0,
            }
            assert sum(m["attempts"] for m in summary["methods"].values()) == 10
            assert summary["duration"] < sum(a["duration"] for a in result["agents"])
            assert summary["estimated_cost"] == 0 and summary["result_kind"].startswith("mock")
            assert summary["detection_metrics"] is None
            for agent in result["agents"]:
                checks = {c["id"]: c for c in agent["checks"]}
                assert checks["own_order_A"]["verdict"] == checks["own_order_B"]["verdict"] == "pass"
                assert checks["cross_account"]["verdict"] == ("vulnerable" if vulnerabilities else "pass")
                assert checks["authentication"]["verdict"] == "pass"
                for evidence in agent["evidence"]:
                    raw = (tmp_path / evidence["artifact"]).read_bytes()
                    assert hashlib.sha256(raw).hexdigest() == evidence["sha256"]
                    assert b"Bearer" not in raw and b"fixture-a" not in raw
                assert {a["evidence_id"] for a in agent["attempts"]} == {e["id"] for e in agent["evidence"]}
            if vulnerabilities:
                assert len(summary["findings"][0]["agent_run_ids"]) == 2
                assert len(summary["findings"][0]["evidence_ids"]) == 4
            first = (await client.get(f"/executions/{execution_id}/logs?limit=2")).json()
            second = (await client.get(f"/executions/{execution_id}/logs?after={first['next_after']}&limit=2")).json()
            assert second["events"][0]["sequence"] == first["next_after"] + 1
            # Completed cancellation is idempotent and does not rewrite the result.
            assert (await client.post(f"/executions/{execution_id}/cancel")).json()["status"] == "completed"
        async with client_for(tmp_path) as (client, _):
            assert (await client.get(f"/executions/{execution_id}")).json()["status"] == "completed"
            assert (await client.get(f"/executions/{execution_id}/report")).json() == summary
    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"collaboration": "debate"}, {"information": "black-box"}, {"tools": ["browser"]},
    {"state_changes": True}, {"url": "https://example.com"}, {"schedule": "tomorrow"},
    {"agents": [{"model": "mock/explorer-a"}]},
    {"agents": [{"model": "mock/explorer-a"}, {"model": "mock/explorer-a"}]},
    {"agents": [{"model": "unknown-a"}, {"model": "unknown-b"}]},
    {"mode": "live"}, {"budget": {"requests": 0}},
])
def test_unsupported_rejected(tmp_path, payload):
    async def run():
        async with client_for(tmp_path) as (client, _):
            assert (await client.post("/executions", json=payload)).status_code == 422
            assert (await client.get("/executions/missing")).status_code == 404
    asyncio.run(run())


@pytest.mark.parametrize("path", ["http://evil/orders/101", "//evil/orders/101", "/orders/../health",
                                   "/orders/%31%30%31", "/orders/101?url=http://evil", "/orders/101#x"])
def test_gateway_scope(path):
    async def run():
        gateway = ToolGateway(create_fixture("vulnerable"))
        try:
            with pytest.raises(ValueError, match="scope_denied"):
                await gateway.execute(ToolRequest(path=path, account="A"))
        finally:
            await gateway.close()
    asyncio.run(run())


def test_http_200_is_not_enough():
    attempt = SimpleNamespace(request=ToolRequest(path="/orders/202", account="A"))
    for body in ({}, {"detail": "forbidden"}, {"id": "202", "owner": "B"}):
        result = evaluate(attempt, {"status_code": 200, "body": body})
        assert result["reproduced"] is None and result["verdict"] == "needs_review"
    assert evaluate(attempt, {"status_code": 500, "body": {}})["reproduced"] is None


class BrokenAfterEvidence(MockProvider):
    async def complete(self, spec, messages, output_tokens, timeout):
        observed = json.loads(messages[-1]["content"])["observations"]
        if spec.id.endswith("a") and observed:
            return Reply("not JSON", 7, 2)
        return await super().complete(spec, messages, output_tokens, timeout)


def test_partial_failure_and_malformed_output(tmp_path):
    async def run():
        async with client_for(tmp_path, provider=BrokenAfterEvidence()) as (client, _):
            execution_id = await launch(client)
            data = await done(client, execution_id)
            assert data["status"] == "failed" and data["partial_failure"] is True
            a, b = data["agents"]
            assert a["status"] == "failed" and a["error"] == "ValidationError"
            assert len(a["evidence"]) == 1 and a["token_usage"] == 9
            assert b["status"] == "completed" and len(b["evidence"]) == 5
            summary = (await client.get(f"/executions/{execution_id}/report")).json()
            assert summary["comparison"]["complete"] is False
    asyncio.run(run())


class SlowAfterEvidence(MockProvider):
    def __init__(self):
        self.waiting = asyncio.Event()
        self.waiters = set()

    async def complete(self, spec, messages, output_tokens, timeout):
        observed = json.loads(messages[-1]["content"])["observations"]
        if observed:
            self.waiters.add(spec.id)
            if len(self.waiters) == 2:
                self.waiting.set()
            await asyncio.sleep(10)
        return await super().complete(spec, messages, output_tokens, timeout)


def test_cancel_preserves_evidence_and_concurrency(tmp_path):
    async def run():
        provider = SlowAfterEvidence()
        async with client_for(tmp_path, provider=provider) as (client, _):
            execution_id = await launch(client)
            await asyncio.wait_for(provider.waiting.wait(), 1)
            assert (await client.post("/executions", json={})).status_code == 409
            response = await client.post(f"/executions/{execution_id}/cancel")
            data = response.json()
            assert data["status"] == "cancelled"
            assert all(a["status"] == "cancelled" for a in data["agents"])
            assert all(len(a["evidence"]) == 1 for a in data["agents"])
            assert all(a["estimated_cost"] is None for a in data["agents"])
            assert any(e["type"] == "cancellation_processed" for e in data["events"])
            await asyncio.sleep(0.02)
            assert (await client.get(f"/executions/{execution_id}")).json() == data
    asyncio.run(run())


def test_timeouts_preserve_partial_results(tmp_path):
    async def run():
        async with client_for(tmp_path, provider=SlowAfterEvidence(), call_timeout=0.1) as (client, _):
            execution_id = await launch(client)
            data = await done(client, execution_id)
            assert data["status"] == "failed"
            for agent in data["agents"]:
                assert agent["error"] == "TimeoutError" and len(agent["evidence"]) == 1
                assert agent["model_calls"] == 3  # first success, timeout, one retry
                assert agent["estimated_cost"] is None and not agent["token_usage_complete"]
                assert agent["token_budget_consumed"] > 0
    asyncio.run(run())


@pytest.mark.parametrize("budget,reason", [({"requests": 1}, "requests"), ({"model_calls": 1}, "model_calls"),
                                         ({"tokens": 100}, "tokens")])
def test_budget_limits(tmp_path, budget, reason):
    async def run():
        async with client_for(tmp_path) as (client, _):
            data = await done(client, await launch(client, {"budget": budget}))
            assert all(a["termination_reason"] == f"budget_exhausted:{reason}" for a in data["agents"])
            assert all(a["requests"] <= budget.get("requests", 12) for a in data["agents"])
            assert all(a["model_calls"] <= budget.get("model_calls", 12) for a in data["agents"])
            assert all(c["verdict"] is None for a in data["agents"] for c in a["checks"] if c["progress"] == "not_run")
    asyncio.run(run())


def test_retry_and_isolation(tmp_path):
    class RetryProvider(MockProvider):
        def __init__(self):
            self.calls = {}
            self.contexts = {}

        async def complete(self, spec, messages, output_tokens, timeout):
            self.calls[spec.id] = self.calls.get(spec.id, 0) + 1
            context = json.loads(messages[-1]["content"])
            self.contexts.setdefault(spec.id, []).append(context)
            assert "checks" not in context and "findings" not in context and "agents" not in context
            if self.calls[spec.id] == 1:
                raise ConnectionError("fake secret must not reach logs")
            return await super().complete(spec, messages, output_tokens, timeout)
    async def run():
        provider = RetryProvider()
        async with client_for(tmp_path, provider=provider) as (client, _):
            data = await done(client, await launch(client))
            assert data["status"] == "completed"
            assert all(a["model_calls"] == 7 and a["estimated_cost"] is None for a in data["agents"])
            assert "fake secret" not in json.dumps(data)
            assert all(contexts[0]["observations"] == [] for contexts in provider.contexts.values())
            assert provider.contexts["mock/explorer-a"][2]["observations"][0]["request"]["account"] == "A"
            assert provider.contexts["mock/explorer-b"][2]["observations"][0]["request"]["account"] == "B"
        left, right = create_fixture("fixed"), create_fixture("fixed")
        assert left is not right
        one, two = ToolGateway(left), ToolGateway(right)
        try:
            a = await one.execute(ToolRequest(path="/orders/101", account="A"))
            a["body"]["item"] = "modified observation"
            b = await two.execute(ToolRequest(path="/orders/101", account="A"))
            assert b["body"]["item"] == "notebook"
        finally:
            await one.close()
            await two.close()
    asyncio.run(run())


def test_evaluator_failure_is_not_pass(tmp_path):
    def fail(attempt, observation):
        raise RuntimeError("oracle unavailable")
    async def run():
        async with client_for(tmp_path, evaluator=fail) as (client, _):
            data = await done(client, await launch(client))
            assert data["status"] == "failed"
            for agent in data["agents"]:
                assert len(agent["evidence"]) == 1
                assert agent["attempts"][0]["reproduced"] is None
                assert agent["attempts"][0]["error"] == "RuntimeError"
                assert all(c["verdict"] is None for c in agent["checks"])
    asyncio.run(run())


def test_pending_cancel_and_restart(tmp_path):
    async def run():
        store = Store(tmp_path)
        manager = Manager(store, registry())
        pending = manager.create(ExecutionRequest())
        cancelled = await manager.cancel(pending.id)
        assert cancelled.status == "cancelled"
        assert all(a.status == "cancelled" for a in cancelled.agents)
        interrupted = manager.create(ExecutionRequest())
        # Simulate process loss before its task runs; persisted state remains pending.
        task = manager.tasks.pop(interrupted.id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        manager.active.clear()
        store.close()
        async with client_for(tmp_path) as (client, _):
            data = (await client.get(f"/executions/{interrupted.id}")).json()
            assert data["status"] == "failed" and data["error"] == "server_restart"
            assert all(a["status"] == "failed" for a in data["agents"])
            assert (await client.get(f"/executions/{cancelled.id}")).json()["status"] == "cancelled"
    asyncio.run(run())


def test_shutdown_marks_failed(tmp_path):
    async def run():
        provider = SlowAfterEvidence()
        async with client_for(tmp_path, provider=provider) as (client, _):
            execution_id = await launch(client)
            await asyncio.wait_for(provider.waiting.wait(), 1)
        async with client_for(tmp_path) as (client, _):
            data = (await client.get(f"/executions/{execution_id}")).json()
            assert data["status"] == "failed" and data["error"] == "server_shutdown"
            assert all(len(a["evidence"]) == 1 for a in data["agents"])
    asyncio.run(run())


@pytest.mark.parametrize("model,key", [("openai/gpt-4o-mini", "OPENAI_API_KEY"),
                                      ("anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY"),
                                      ("gemini/gemini-3.6-flash", "GEMINI_API_KEY")])
def test_litellm_adapter_without_network(monkeypatch, model, key):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm
    seen = {}
    async def fake(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"stop","hypothesis":"done"}'))],
                               usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5))
    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setenv(key, "private-test-key")
    monkeypatch.setenv("MODEL_A", model)
    spec = registry()[model]
    reply = asyncio.run(LiteLLMProvider().complete(spec, [{"role": "user", "content": "JSON"}], 100, 2))
    assert reply.input_tokens == 11 and reply.output_tokens == 5
    assert seen["api_key"] == "private-test-key"
    assert seen["num_retries"] == seen["max_retries"] == 0
    assert seen["timeout"] == 2


def test_gemini_registry(monkeypatch):
    monkeypatch.setenv("MODEL_A", "gemini/gemini-3.6-flash")
    monkeypatch.setenv("MODEL_B", "gemini/gemini-3.5-flash-lite")
    monkeypatch.setenv("MODEL_A_INPUT_USD_PER_MILLION", "")
    models = registry()
    assert models["gemini/gemini-3.6-flash"].key_env == "GEMINI_API_KEY"
    assert models["gemini/gemini-3.5-flash-lite"].key_env == "GEMINI_API_KEY"
    assert models["gemini/gemini-3.6-flash"].input_rate is None
    monkeypatch.setenv("MODEL_A", "unknown/model")
    with pytest.raises(ValueError, match="Unsupported provider"):
        registry()


def test_runner_error_is_not_non_reproduction(tmp_path, monkeypatch):
    async def failure(self, request):
        raise httpx.ReadTimeout("local fixture timed out")
    monkeypatch.setattr(ToolGateway, "execute", failure)
    async def run():
        async with client_for(tmp_path) as (client, _):
            execution_id = await launch(client)
            data = await done(client, execution_id)
            assert data["status"] == "failed"
            for agent in data["agents"]:
                assert agent["attempts"][0]["error"] == "ReadTimeout"
                assert agent["attempts"][0]["reproduced"] is None
                assert agent["findings"] == []
            summary = (await client.get(f"/executions/{execution_id}/report")).json()
            assert summary["methods"]["http_order_access"]["errors"] == 2
            assert summary["methods"]["http_order_access"]["not_reproduced"] == 0
    asyncio.run(run())


def test_total_deadline_and_restart_running_evidence(tmp_path):
    async def deadline():
        async with client_for(tmp_path / "deadline", provider=SlowAfterEvidence()) as (client, _):
            data = await done(client, await launch(client, {"budget": {"seconds": 0.15}}))
            assert data["status"] == "failed"
            assert all(a["error"] == "TimeoutError" for a in data["agents"])
    asyncio.run(deadline())
    async def crash():
        provider = SlowAfterEvidence()
        directory = tmp_path / "restart"
        async with client_for(directory, provider=provider) as (client, app):
            execution_id = await launch(client)
            await asyncio.wait_for(provider.waiting.wait(), 1)
            snapshot = app.state.store.get(execution_id)
        # Restore the snapshot a hard-killed process would leave on disk.
        store = Store(directory)
        store.save(snapshot)
        store.close()
        async with client_for(directory) as (client, _):
            data = (await client.get(f"/executions/{execution_id}")).json()
            assert data["status"] == "failed" and data["error"] == "server_restart"
            for agent in data["agents"]:
                assert len(agent["evidence"]) == 1
                assert agent["model_attempts"][-1]["error"] == "server_restart"
                assert agent["estimated_cost"] is None and agent["token_usage_complete"] is False
                assert (directory / agent["evidence"][0]["artifact"]).exists()
    asyncio.run(crash())


def test_cost_reservation_and_unknown_cost_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    payload = {"mode": "live", "agents": [{"model": "openai/gpt-4o-mini"}, {"model": "anthropic/claude-haiku-4-5"}],
               "budget": {"cost_usd": 0.000001}}
    async def unknown():
        async with client_for(tmp_path) as (client, _):
            assert (await client.post("/executions", json=payload)).status_code == 422
    asyncio.run(unknown())
    for key in ("MODEL_A_INPUT_USD_PER_MILLION", "MODEL_A_OUTPUT_USD_PER_MILLION",
                "MODEL_B_INPUT_USD_PER_MILLION", "MODEL_B_OUTPUT_USD_PER_MILLION"):
        monkeypatch.setenv(key, "10")
    async def capped():
        async with client_for(tmp_path, provider=MockProvider()) as (client, _):
            data = await done(client, await launch(client, payload))
            assert all(a["model_calls"] == 0 and a["termination_reason"] == "budget_exhausted:cost" for a in data["agents"])
    asyncio.run(capped())
