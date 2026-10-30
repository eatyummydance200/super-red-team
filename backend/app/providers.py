import asyncio
import json
import os
import math
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import urlsplit

import httpx

from .contracts import Decision


@dataclass(frozen=True)
class ModelSpec:
    id: str
    mode: str
    key_env: str | None = None
    input_rate: float | None = None
    output_rate: float | None = None
    name: str = ""
    provider: str = ""
    base_url: str | None = None
    parameters: dict = field(default_factory=dict)

    def reservation_cost(self, input_tokens, output_tokens):
        if self.mode == "mock":
            return 0.0
        if self.input_rate is None or self.output_rate is None:
            return None
        return (input_tokens * self.input_rate + output_tokens * self.output_rate) / 1_000_000


def registry():
    def rate(name):
        value = os.getenv(name)
        if not value:
            return None
        result = float(value)
        if not math.isfinite(result) or not 0 <= result < 1_000_000:
            raise ValueError(f"Invalid rate: {name}")
        return result
    def live_model(slot, default):
        model = os.getenv(f"MODEL_{slot}", default)
        compatible = {
            "deepseek-flash": ("DeepSeek Flash", "DeepSeek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
            "qwen3.8-flash": ("Qwen 3.8 Flash", "Qwen", "QWEN_API_KEY", "https://ap-southeast-1.maas.qwencloudapi.com/compatible-mode/v1", {"enable_thinking": True}),
            "glm-5.3-flash": ("GLM 5.3 Flash", "Z.AI", "ZAI_API_KEY", "https://api.z.ai/api/paas/v4", {"thinking": {"type": "enabled"}}),
        }
        if model in compatible:
            name, provider, key_env, base_url, parameters = compatible[model]
            base_url = os.getenv(f"MODEL_{slot}_BASE_URL", base_url).rstrip("/")
            url = urlsplit(base_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f"Invalid HTTPS Base URL for MODEL_{slot}")
            return ModelSpec(model, "live", key_env, rate(f"MODEL_{slot}_INPUT_USD_PER_MILLION"),
                             rate(f"MODEL_{slot}_OUTPUT_USD_PER_MILLION"), name, provider, base_url, parameters)
        key_env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}.get(model.split("/", 1)[0])
        if key_env is None or "/" not in model:
            raise ValueError(f"Unsupported provider for MODEL_{slot}")
        return ModelSpec(model, "live", key_env, rate(f"MODEL_{slot}_INPUT_USD_PER_MILLION"),
                         rate(f"MODEL_{slot}_OUTPUT_USD_PER_MILLION"), model.split("/", 1)[1], model.split("/", 1)[0])

    return {s.id: s for s in [
        *(ModelSpec(f"mock/explorer-{slot}", "mock", name=f"Mock Explorer {slot.upper()}", provider="Mock") for slot in "abc"),
        live_model("A", "deepseek-flash"),
        live_model("B", "qwen3.8-flash"),
        live_model("C", "glm-5.3-flash")
    ]}


@dataclass
class Reply:
    content: str
    input_tokens: int
    output_tokens: int


class MockProvider:
    async def complete(self, spec, messages, output_tokens, timeout):
        await asyncio.sleep(0.01)
        context = json.loads(messages[-1]["content"])
        observations = context["observations"]
        seen = {(o["request"]["account"], o["request"]["path"]) for o in observations}
        probes = [("A", "/orders/101"), ("B", "/orders/202"),
                  ("A", "/orders/202"), ("B", "/orders/101"), ("anonymous", "/orders/101")]
        if spec.id.endswith("b"):
            probes = [probes[1], probes[0], probes[3], probes[2], probes[4]]
        if "target" in context:
            resources = context["target"]["resources"]
            probes = [(r["owner"], r["path"]) for r in resources]
            probes += [(account, r["path"]) for r in resources
                       for account in ("B" if r["owner"] == "A" else "A", "anonymous")]
        next_probe = next((p for p in probes if p not in seen), None)
        if next_probe:
            account, path = next_probe
            decision = {"action": "http", "hypothesis": "Check order visibility for this identity using observed baselines",
                        "request": {"method": "GET", "path": path, "account": account}}
        else:
            decision = {"action": "stop", "hypothesis": "Baseline, cross-account and anonymous probes collected"}
        return Reply(json.dumps(decision), 0, 0)


class LiteLLMProvider:
    async def complete(self, spec, messages, output_tokens, timeout):
        if spec.base_url:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.post(spec.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + os.environ[spec.key_env]},
                    json={"model": spec.id, "messages": messages, "max_tokens": output_tokens,
                          "stream": False, **spec.parameters})
                response.raise_for_status()
                data = response.json()
            usage = data.get("usage") or {}
            # completion_tokens includes billed reasoning; never add its detail count twice.
            counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
            if any(type(n) is not int or n < 0 for n in counts):
                raise ValueError("missing_or_invalid_provider_usage")
            return Reply(data["choices"][0]["message"].get("content") or "", *counts)
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        response = await litellm.acompletion(
            model=spec.id, api_key=os.environ[spec.key_env], messages=messages,
            max_tokens=output_tokens, timeout=timeout, num_retries=0, max_retries=0,
        )
        content = response.choices[0].message.content
        if response.usage is None:
            raise ValueError("missing_provider_usage")
        return Reply(content or "", response.usage.prompt_tokens, response.usage.completion_tokens)


def parse_decision(content: str):
    if len(content.encode("utf-8")) > 32_768:
        raise ValueError("model_output_too_large")
    return Decision.model_validate_json(content)


def transient(error):
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {429, 500, 502, 503, 504}
    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    return isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {
        "RateLimitError", "ServiceUnavailableError", "APIConnectionError", "Timeout", "InternalServerError"
    }
