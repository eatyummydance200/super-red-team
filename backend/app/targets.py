"""Operator-registered loopback targets. Credentials never enter execution contracts."""
import ipaddress
import json
import os
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from .contracts import Contract


def normalize_target_url(value):
    value = value.strip()
    url = urlsplit(value)
    port = url.port
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or "?" in value or "#" in value
            or any(c.isspace() for c in value) or any(c in value for c in ("\\", "%"))
            or any(p in {".", ".."} for p in url.path.split("/"))):
        raise ValueError("Enter a registered HTTP(S) site URL without credentials, query or fragment")
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
        host += f":{port}"
    return f"{url.scheme}://{host}{url.path.rstrip('/')}"


def local_path(value):
    parts = urlsplit(value)
    if (not value.startswith("/") or value.startswith("//") or parts.scheme or parts.netloc
            or parts.fragment or any(c in value for c in ("\\", "%", "\r", "\n", "\t", " "))
            or any(p in {".", ".."} for p in parts.path.split("/"))):
        raise ValueError("Only literal origin-relative paths and queries are supported")
    return value


class Login(Contract):
    path: str
    fields_env: dict[str, str] = Field(min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)
    csrf_field: str | None = None
    verify_path: str
    success_marker: str = Field(min_length=1)


class Resource(Contract):
    path: str
    owner: Literal["A", "B"]
    marker: str = Field(min_length=8, max_length=500)


class WebTarget(Contract):
    base_url: str
    name: str = ""
    environment: Literal["vulnerable", "fixed"] = "vulnerable"
    urls: list[str] = Field(default_factory=list, max_length=10)
    allowed_paths: list[str] = Field(min_length=1, max_length=30)
    resources: list[Resource] = Field(min_length=2, max_length=20)
    accounts: dict[Literal["A", "B"], Login]

    @model_validator(mode="after")
    def scope(self):
        url = urlsplit(self.base_url)
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
            port = url.port
        except ValueError:
            loopback, port = False, None
        if (url.scheme not in {"http", "https"} or not loopback or not port
                or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("Target must be an explicit loopback IP origin with a port")
        self.base_url = self.base_url.rstrip("/")
        self.urls = list(dict.fromkeys(normalize_target_url(value) for value in self.urls))
        for path in self.allowed_paths:
            local_path(path)
        if set(self.accounts) != {"A", "B"} or {r.owner for r in self.resources} != {"A", "B"}:
            raise ValueError("Register two accounts and an owned resource for each")
        if len({r.path for r in self.resources}) != len(self.resources):
            raise ValueError("Resource paths must be unique")
        if len({r.marker for r in self.resources}) != len(self.resources):
            raise ValueError("Private content markers must be unique per resource")
        for resource in self.resources:
            if resource.path not in self.allowed_paths:
                raise ValueError("Resource path is outside allowed_paths")
        for login in self.accounts.values():
            local_path(login.path)
            local_path(login.verify_path)
        return self


def load_targets():
    filename = os.getenv("REDTEAM_TARGETS_FILE")
    if not filename:
        return {}
    raw = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
    targets = {key: WebTarget.model_validate(value) for key, value in raw.items()}
    seen = set()
    for target in targets.values():
        urls = {normalize_target_url(target.base_url), *target.urls}
        if seen & urls:
            raise ValueError("Each target URL must belong to exactly one registered target")
        seen.update(urls)
    return targets


class HiddenFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type", "").lower() == "hidden" and "name" in attrs:
            self.values[attrs["name"]] = attrs.get("value", "")


class WebGateway:
    def __init__(self, target, transport=None):
        self.target = target
        self.authenticated = set()
        self.secrets = []
        self.login_fields = {}
        for account, login in target.accounts.items():
            self.login_fields[account] = {}
            for field, name in login.fields_env.items():
                value = os.getenv(name)
                if not value:
                    raise ValueError("Missing target credential environment variable")
                self.secrets.append(value)
                self.login_fields[account][field] = value
        self.clients = {account: httpx.AsyncClient(base_url=target.base_url, transport=transport,
                        follow_redirects=False, timeout=5, trust_env=False)
                        for account in ("A", "B", "anonymous")}

    async def response(self, client, method, path, **kwargs):
        # Bound decompressed bytes while streaming, including login responses.
        async with client.stream(method, path, **kwargs) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 32_768:
                    raise ValueError("response_too_large")
            return response.status_code, bytes(data).decode("utf-8", errors="replace")

    async def login(self, account):
        client, config = self.clients[account], self.target.accounts[account]
        fields = dict(config.fields)
        if config.csrf_field:
            status, body = await self.response(client, "GET", config.path)
            parser = HiddenFields()
            parser.feed(body)
            token = parser.values.get(config.csrf_field)
            if status != 200 or not token:
                raise ValueError("login_csrf_missing")
            fields[config.csrf_field] = token
            self.secrets.append(token)
        fields.update(self.login_fields[account])
        status, _ = await self.response(client, "POST", config.path, data=fields)
        if status not in {200, 302, 303}:
            raise ValueError("login_failed")
        # Never follow a redirect; verify the configured local page instead.
        status, body = await self.response(client, "GET", config.verify_path)
        if status != 200 or config.success_marker not in body:
            raise ValueError("login_verification_failed")
        self.authenticated.add(account)

    async def execute(self, request):
        local_path(request.path)
        if request.path not in self.target.allowed_paths:
            raise ValueError("scope_denied")
        start = time.monotonic()
        if request.account != "anonymous" and request.account not in self.authenticated:
            await self.login(request.account)
        client = self.clients[request.account]
        if request.account == "anonymous":
            client.cookies.clear()
        status, body = await self.response(client, "GET", request.path)
        secrets = self.secrets + [cookie.value for c in self.clients.values() for cookie in c.cookies.jar]
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                body = body.replace(secret, "[REDACTED]")
        return {"request": request.model_dump(), "status_code": status,
                "body": {"text": body}, "duration": time.monotonic() - start}

    async def close(self):
        for client in self.clients.values():
            await client.aclose()


def evaluate_web(target, attempt, observation, previous):
    request = attempt.request
    resource = next((r for r in target.resources if r.path == request.path), None)
    if resource is None:
        return {"check": None, "verdict": "needs_review", "reproduced": None,
                "reason": "Exploratory response has no registered ownership marker"}
    status, body = observation["status_code"], observation["body"]["text"]
    exposed = resource.marker in body
    baseline = any(o["request"]["path"] == resource.path and o["request"]["account"] == resource.owner
                   and o["status_code"] == 200 and resource.marker in o["body"].get("text", "") for o in previous)
    if request.account == resource.owner:
        exposed = exposed and status == 200
        return {"check": f"web_access:{request.account}:{request.path}", "verdict": "pass" if exposed else "needs_review",
                "reproduced": None, "reason": "Owner baseline confirmed" if exposed else "Owner baseline missing"}
    reproduced = (True if exposed else False if status in {401, 403, 404} else None) if baseline else None
    return {"check": f"web_access:{request.account}:{request.path}", "verdict": "vulnerable" if reproduced is True else "pass" if reproduced is False else "needs_review",
            "reproduced": reproduced,
            "finding_key": "web-access:" + resource.path,
            "reason": "Registered private content exposed" if reproduced else "Denied or inconclusive; owner baseline required"}
