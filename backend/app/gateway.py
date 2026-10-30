import re
import time

import httpx

from .contracts import ToolRequest


class ToolGateway:
    def __init__(self, fixture, timeout: float = 5):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture),
                                       base_url="http://fixture", follow_redirects=False,
                                       timeout=timeout, trust_env=False)

    async def execute(self, request: ToolRequest) -> dict:
        # Both gateway and runner restrict paths; ASGITransport cannot open sockets.
        if not re.fullmatch(r"/orders/[0-9]{1,6}", request.path):
            raise ValueError("scope_denied: only /orders/{numeric_id} is registered")
        headers = {}
        if request.account != "anonymous":
            headers["Authorization"] = {"A": "Bearer fixture-a", "B": "Bearer fixture-b"}[request.account]
        start = time.monotonic()
        response = await self.client.get(request.path, headers=headers)
        if 300 <= response.status_code < 400:
            raise ValueError("redirect_denied")
        if len(response.content) > 32_768:
            raise ValueError("response_too_large")
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:4000]}
        return {"request": request.model_dump(), "status_code": response.status_code,
                "body": body, "duration": time.monotonic() - start}

    async def close(self):
        await self.client.aclose()
