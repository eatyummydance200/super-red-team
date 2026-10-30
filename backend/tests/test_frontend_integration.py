import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.evaluator import evaluate
from app.main import create_app
from app.providers import LiteLLMProvider, MockProvider, registry
from app.report import report


@asynccontextmanager
async def api(path, **kwargs):
    app = create_app(path, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
            yield client, app


def test_three_model_api_and_report(tmp_path):
    async def run():
        async with api(tmp_path) as (client, app):
            models = (await client.get("/models")).json()
            assert len([m for m in models if m["mode"] == "mock"]) == 3
            assert all("name" in m and "input_per_mtok" in m for m in models)
            created = await client.post("/executions", json={})
            execution_id = created.json()["execution_id"]
            assert created.json()["is_mock"] is True
            before = (await client.get(f"/executions/{execution_id}/report")).json()
            assert before["is_final"] is False
            await app.state.manager.tasks[execution_id]
            state = (await client.get(f"/executions/{execution_id}")).json()
            assert len(state["agents"]) == 3 and state["status"] == "completed"
            assert all(a["progress"] == 1 and a["phase"] == "completed" for a in state["agents"])
            result = (await client.get(f"/executions/{execution_id}/report")).json()
            assert result["summary"]["attempts"] == 15
            assert result["summary"]["reproduced"] == 6
            assert len(result["comparison"]["agents"]) == 3
            assert all(a["common_count"] == 1 and a["only_count"] == 0 for a in result["comparison"]["agents"])
            assert len(result["combinations"]) == 7 and sum(c["is_current"] for c in result["combinations"]) == 1
            assert all(c["score"] is None for c in result["combinations"])
            assert result["findings"][0]["cvss"] is None
            assert result["findings"][0]["reproduce_rate"] == 1
            assert result["findings"][0]["verdict"] == "confirmed"
            assert len(result["method_results"]) == 12 and len(result["checklist"]) == 4
            assert len(result["attempts"]) == len(result["evidence"]) == 15
            assert sum(r["tokens"] for r in result["method_results"]) == 0
            logs = (await client.get(f"/executions/{execution_id}/logs")).json()
            assert all(e["message"] and e["level"] in {"info", "warn", "error"} for e in logs["events"])
            assert (await client.get("/executions?limit=1")).json()["executions"][0]["id"] == execution_id
            assert (await client.post(f"/executions/{execution_id}/cancel")).json() == state
        async with api(tmp_path) as (client, _):
            assert (await client.get("/executions")).json()["executions"][0]["id"] == execution_id
    asyncio.run(run())


def test_known_cvss_scoring_and_denominator(tmp_path):
    def rated(attempt, observation):
        result = evaluate(attempt, observation)
        if result["reproduced"]:
            result.update(cvss=7.5, severity="High")
        return result

    async def run():
        async with api(tmp_path, evaluator=rated) as (client, app):
            execution_id = (await client.post("/executions", json={})).json()["execution_id"]
            await app.state.manager.tasks[execution_id]
            execution = app.state.store.get(execution_id)
            baseline = report(execution)
            assert all(c["score"] == 7.5 for c in baseline["combinations"])
            # A qualifying blocked probe counts in the denominator, not as a false positive.
            attempt = next(a for a in execution.agents[0].attempts if a.reproduced)
            attempt.reproduced = False
            result = report(execution)
            assert result["findings"][0]["reproduce_rate"] == 5 / 6
            assert next(c for c in result["combinations"] if c["is_current"])["score"] == 7.5 * 5 / 6
            assert result["summary"]["false_positive_findings"] == 0
    asyncio.run(run())


def test_one_of_three_failure_and_null_usage(tmp_path):
    class BrokenOne(MockProvider):
        async def complete(self, spec, messages, output_tokens, timeout):
            if spec.id.endswith("c"):
                raise ValueError("private provider error must not appear")
            return await super().complete(spec, messages, output_tokens, timeout)

    async def run():
        async with api(tmp_path, provider=BrokenOne()) as (client, app):
            execution_id = (await client.post("/executions", json={})).json()["execution_id"]
            await app.state.manager.tasks[execution_id]
            state = (await client.get(f"/executions/{execution_id}")).json()
            assert state["status"] == "failed" and state["partial_failure"]
            assert [a["status"] for a in state["agents"]].count("completed") == 2
            assert state["agents"][2]["token_usage"] is None
            result = (await client.get(f"/executions/{execution_id}/report")).json()
            assert result["summary"]["tokens"] is result["summary"]["cost_usd"] is None
            assert result["partial"] and not result["comparison"]["complete"]
            assert next(c for c in result["combinations"] if c["is_current"])["score"] is None
            assert "private provider error" not in json.dumps(result)
    asyncio.run(run())


def test_authentication_and_cors(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKEND_API_TOKEN", "integration-secret")
    monkeypatch.setenv("REQUIRE_API_AUTH", "true")
    origin = "https://super-red-team-frontend.vercel.app"

    async def run():
        async with api(tmp_path) as (client, _):
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/models")).status_code == 401
            assert (await client.post("/executions", json={})).status_code == 401
            response = await client.options("/executions", headers={"Origin": origin,
                "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
            headers = {"Authorization": "Bearer integration-secret", "Origin": origin}
            assert (await client.get("/models", headers=headers)).status_code == 200
            denied = await client.options("/executions", headers={"Origin": "https://untrusted.invalid", "Access-Control-Request-Method": "POST"})
            assert denied.status_code == 400 and "access-control-allow-origin" not in denied.headers
    asyncio.run(run())
    monkeypatch.delenv("BACKEND_API_TOKEN")
    with pytest.raises(ValueError, match="BACKEND_API_TOKEN"):
        create_app(tmp_path)


@pytest.mark.parametrize("model,slot,key_env", [("deepseek-flash", "A", "DEEPSEEK_API_KEY"),
    ("qwen3.8-flash", "B", "QWEN_API_KEY"), ("glm-5.3-flash", "C", "ZAI_API_KEY")])
def test_compatible_provider_parameters_and_billed_usage(monkeypatch, model, slot, key_env):
    for name, value in zip("ABC", ("deepseek-flash", "qwen3.8-flash", "glm-5.3-flash")):
        monkeypatch.setenv(f"MODEL_{name}", value)
    monkeypatch.setenv(key_env, "private-key")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":"stop","hypothesis":"done"}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 150, "completion_tokens_details": {"reasoning_tokens": 140}}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    spec = registry()[model]
    reply = asyncio.run(LiteLLMProvider().complete(spec, [{"role": "user", "content": "test"}], 4000, 10))
    assert reply.output_tokens == 150 and reply.input_tokens == 20
    payload = json.loads(seen[0].content)
    assert payload["model"] == model and payload["max_tokens"] == 4000
    assert all(payload[k] == v for k, v in spec.parameters.items())
    assert str(seen[0].url) == spec.base_url + "/chat/completions"


def test_remote_access_without_token_is_denied(tmp_path):
    async def run():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.1", 1234)), base_url="http://backend") as client:
                assert (await client.get("/models")).status_code == 403
    asyncio.run(run())
