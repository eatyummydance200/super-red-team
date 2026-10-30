import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.contracts import ExecutionRequest, ToolRequest
from app.main import create_app
from app.targets import WebGateway, WebTarget, evaluate_web, load_targets, normalize_target_url


def target_config():
    return {
        "base_url": "http://127.0.0.1:18880",
        "allowed_paths": ["/?page=invoice&id=1", "/?page=invoice&id=2", "/redirect", "/large"],
        "resources": [
            {"path": "/?page=invoice&id=1", "owner": "A", "marker": "PRIVATE-INVOICE-111"},
            {"path": "/?page=invoice&id=2", "owner": "B", "marker": "PRIVATE-INVOICE-222"}],
        "accounts": {account: {"path": "/login", "fields_env": {"username": "WEB_" + account},
                    "fields": {"action": "login"}, "csrf_field": "csrf", "verify_path": "/me",
                    "success_marker": "Signed in " + account} for account in ("A", "B")}}


def website(request, vulnerable=True):
    path = request.url.path
    cookie = request.headers.get("cookie", "")
    account = next((a for a in ("A", "B") if "session=session-secret-" + a in cookie), "anonymous")
    if path == "/login" and request.method == "GET":
        return httpx.Response(200, text='<input type="hidden" name="csrf" value="token-123">',
                              headers={"set-cookie": "csrf=token-123; Path=/"})
    if path == "/login":
        fields = parse_qs(request.content.decode())
        assert fields["csrf"] == ["token-123"] and "csrf=token-123" in cookie
        account = fields["username"][0][-1]
        return httpx.Response(302, headers={"set-cookie": f"session=session-secret-{account}; Path=/",
                                           "location": "http://outside.invalid/never-follow"})
    if path == "/me":
        return httpx.Response(200, text="Signed in " + account)
    if path == "/redirect":
        return httpx.Response(302, headers={"location": "http://outside.invalid/never-follow"})
    if path == "/large":
        return httpx.Response(200, content=b"x" * 32769)
    invoice = request.url.params["id"]
    owner = "A" if invoice == "1" else "B"
    if account == "anonymous" or (not vulnerable and account != owner):
        return httpx.Response(403, text="Denied")
    return httpx.Response(200, text="PRIVATE-INVOICE-" + ("111" if invoice == "1" else "222") + " secret-" + account)


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("WEB_A", "secret-A")
    monkeypatch.setenv("WEB_B", "secret-B")


@pytest.mark.parametrize("vulnerable,findings", [(True, 2), (False, 0)])
@pytest.mark.parametrize("selector", ["id", "public_url", "internal_url", "both"])
def test_web_execution(tmp_path, monkeypatch, vulnerable, findings, selector):
    filename = tmp_path / "targets.json"
    config = {**target_config(), "name": "VM A", "urls": ["https://lab.example/lab/a/"],
              "environment": "vulnerable" if vulnerable else "fixed"}
    filename.write_text(json.dumps({"paper-cup": config}))
    monkeypatch.setenv("REDTEAM_TARGETS_FILE", str(filename))
    original_init = WebGateway.__init__
    calls = []

    def handle(request):
        assert request.url.host == "127.0.0.1"
        calls.append(request)
        return website(request, vulnerable)

    monkeypatch.setattr(WebGateway, "__init__", lambda self, target: original_init(self, target, httpx.MockTransport(handle)))

    async def run():
        app = create_app(tmp_path / "data")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
                assert (await client.get("/scenarios")).json()[1]["target_ids"] == ["paper-cup"]
                metadata = (await client.get("/scenarios")).json()[1]
                assert metadata["targets"][0]["url"] == "https://lab.example/lab/a"
                assert metadata["targets"][0]["environment"] == config["environment"]
                selection = {"target_id": "paper-cup"} if selector in {"id", "both"} else {}
                if selector != "id":
                    selection["target_url"] = config["base_url"] if selector == "internal_url" else "https://LAB.example:443/lab/a/"
                response = await client.post("/executions", json={"scenario": "web-access-v1", **selection, "agents": [{"model": "mock/explorer-a"}, {"model": "mock/explorer-b"}]})
                assert response.status_code == 202, response.text
                execution_id = response.json()["execution_id"]
                await app.state.manager.tasks[execution_id]
                report = (await client.get(f"/executions/{execution_id}/report")).json()
                assert report["status"] == "completed"
                assert report["unique_vulnerability_count"] == findings
                assert report["comparison"]["complete"] is True
                assert report["scope"]["target"] == "paper-cup"
                assert report["config"]["environment"] == config["environment"]
                expected_url = None if selector == "id" else normalize_target_url(selection["target_url"])
                assert report["scope"]["target_url"] == report["settings"]["target_url"] == expected_url
                assert all(len(a["attempts"]) == 6 for a in report["agents"])
                assert "registered_web_http" in report["result_kind"]
                serialized = json.dumps(report)
                assert "secret-A" not in serialized and "secret-B" not in serialized
                assert "token-123" not in serialized
                for artifact in (tmp_path / "data").rglob("*.json"):
                    assert "secret-" not in artifact.read_text()
                assert (await client.post("/executions", json={"scenario": "web-access-v1", "target_id": "missing"})).status_code == 422
                count = len(calls)
                for url in ["http://169.254.169.254/", "http://127.0.0.1:8080/", "https://lab.example/lab/ab",
                            "https://lab.example/lab/a?next=http://outside.invalid", "https://lab.example/lab/a#fragment"]:
                    denied = await client.post("/executions", json={"scenario": "web-access-v1", "target_url": url})
                    assert denied.status_code == 422
                denied = await client.post("/executions", json={"scenario": "web-access-v1", "target_id": "other", "target_url": config["urls"][0]})
                assert denied.status_code == 422 and "different sites" in denied.json()["detail"]
                assert len(calls) == count
        assert sum(r.method == "POST" for r in calls) == 4
    asyncio.run(run())


def test_scope_redirect_and_response_limit():
    async def run():
        calls = []

        def handle(request):
            calls.append(request)
            return website(request)

        gateway = WebGateway(WebTarget.model_validate(target_config()), httpx.MockTransport(handle))
        try:
            for path in ("http://outside.invalid/", "//outside.invalid/", "/../me", "/%2e/me", "/?page=unregistered"):
                with pytest.raises(ValueError):
                    await gateway.execute(ToolRequest(path=path, account="anonymous"))
            assert calls == []
            result = await gateway.execute(ToolRequest(path="/redirect", account="anonymous"))
            assert result["status_code"] == 302 and len(calls) == 1
            with pytest.raises(ValueError, match="response_too_large"):
                await gateway.execute(ToolRequest(path="/large", account="anonymous"))
        finally:
            await gateway.close()
    asyncio.run(run())


def test_login_failure_and_missing_credentials(monkeypatch):
    target = WebTarget.model_validate(target_config())

    async def run():
        gateway = WebGateway(target, httpx.MockTransport(lambda request: httpx.Response(200, text="Login form")))
        try:
            with pytest.raises(ValueError, match="login_csrf_missing"):
                await gateway.execute(ToolRequest(path=target.resources[0].path, account="A"))
        finally:
            await gateway.close()
        gateway = WebGateway(target, httpx.MockTransport(lambda request: httpx.Response(200, text='<input type="hidden" name="csrf" value="x">')))
        try:
            with pytest.raises(ValueError, match="login_verification_failed"):
                await gateway.execute(ToolRequest(path=target.resources[0].path, account="A"))
        finally:
            await gateway.close()
    asyncio.run(run())
    monkeypatch.delenv("WEB_A")
    with pytest.raises(ValueError, match="Missing target credential"):
        WebGateway(target)


def test_evaluator_requires_baseline_and_private_marker():
    target = WebTarget.model_validate(target_config())
    path = target.resources[0].path
    attempt = SimpleNamespace(request=ToolRequest(path=path, account="B"))
    observation = {"request": {"path": path, "account": "A"}, "status_code": 200,
                   "body": {"text": target.resources[0].marker}}
    assert evaluate_web(target, attempt, observation, [])["reproduced"] is None
    assert evaluate_web(target, attempt, observation, [observation])["reproduced"] is True
    for status in (200, 302, 500):
        assert evaluate_web(target, attempt, {**observation, "status_code": status, "body": {"text": "Login"}}, [observation])["reproduced"] is None
    assert evaluate_web(target, attempt, {**observation, "status_code": 403}, [observation])["reproduced"] is True
    assert evaluate_web(target, attempt, {**observation, "status_code": 403, "body": {"text": "Denied"}}, [observation])["reproduced"] is False


def test_real_loopback_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            content = self.rfile.read(int(self.headers.get("content-length", 0)))
            request = httpx.Request(self.command, f"http://127.0.0.1:{self.server.server_port}" + self.path,
                                    headers=dict(self.headers), content=content)
            response = website(request)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        config = target_config()
        config["base_url"] = f"http://127.0.0.1:{server.server_port}"
        gateway = WebGateway(WebTarget.model_validate(config))
        try:
            for account, expected in (("A", 200), ("B", 200), ("anonymous", 403)):
                result = await gateway.execute(ToolRequest(path=config["resources"][0]["path"], account=account))
                assert result["status_code"] == expected
                assert "secret-" not in result["body"]["text"]
        finally:
            await gateway.close()
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("origin", ["http://example.com:80", "http://169.254.169.254:80", "http://127.0.0.1:80/path", "http://user:pass@127.0.0.1:80", "http://127.0.0.1"])
def test_target_origin_validation(origin):
    with pytest.raises(ValueError):
        WebTarget.model_validate({**target_config(), "base_url": origin})


def test_target_contract():
    with pytest.raises(ValueError):
        ExecutionRequest(scenario="web-access-v1")
    with pytest.raises(ValueError):
        ExecutionRequest(target_id="paper-cup")
    with pytest.raises(ValueError):
        ExecutionRequest(target_url="https://lab.example/lab/a")
    assert ExecutionRequest(scenario="web-access-v1", target_url="https://lab.example/lab/a").target_id is None
    config = target_config()
    config["resources"][0]["path"] = "/outside"
    with pytest.raises(ValueError):
        WebTarget.model_validate(config)


@pytest.mark.parametrize("url", ["ftp://lab.example/", "https://user:pass@lab.example/", "https://lab.example/?x=1",
                                 "https://lab.example/#x", "https://lab.example/lab/%61", "https://lab.example/lab/../a",
                                 "https://lab.example:bad/", "https://lab.example\\@outside.invalid/", "https://lab.exa mple/"])
def test_target_url_validation(url):
    with pytest.raises(ValueError):
        WebTarget.model_validate({**target_config(), "urls": [url]})


def test_target_url_normalization_and_uniqueness(tmp_path, monkeypatch):
    assert normalize_target_url(" HTTPS://LAB.EXAMPLE:443/lab/a/ ") == "https://lab.example/lab/a"
    assert normalize_target_url("http://[::1]:18880/") == "http://[::1]:18880"
    config = {**target_config(), "urls": ["https://lab.example/lab/a/"]}
    filename = tmp_path / "targets.json"
    filename.write_text(json.dumps({"a": config, "b": {**config, "base_url": "http://127.0.0.1:18882"}}))
    monkeypatch.setenv("REDTEAM_TARGETS_FILE", str(filename))
    with pytest.raises(ValueError, match="exactly one"):
        load_targets()
