import asyncio
import json
import os
from dataclasses import dataclass

from .contracts import Decision


@dataclass(frozen=True)
class ModelSpec:
    id: str
    mode: str
    key_env: str | None = None
    input_rate: float | None = None
    output_rate: float | None = None

    def reservation_cost(self, input_tokens, output_tokens):
        if self.mode == "mock":
            return 0.0
        if self.input_rate is None or self.output_rate is None:
            return None
        return (input_tokens * self.input_rate + output_tokens * self.output_rate) / 1_000_000


def registry():
    def rate(name):
        value = os.getenv(name)
        if value is None:
            return None
        result = float(value)
        if not 0 <= result < 1_000_000:
            raise ValueError(f"Invalid rate: {name}")
        return result
    return {s.id: s for s in [
        ModelSpec("mock/explorer-a", "mock"), ModelSpec("mock/explorer-b", "mock"),
        ModelSpec(os.getenv("MODEL_A", "openai/gpt-4o-mini"), "live", "OPENAI_API_KEY",
                  rate("MODEL_A_INPUT_USD_PER_MILLION"), rate("MODEL_A_OUTPUT_USD_PER_MILLION")),
        ModelSpec(os.getenv("MODEL_B", "anthropic/claude-haiku-4-5"), "live", "ANTHROPIC_API_KEY",
                  rate("MODEL_B_INPUT_USD_PER_MILLION"), rate("MODEL_B_OUTPUT_USD_PER_MILLION"))
    ]}


@dataclass
class Reply:
    content: str
    input_tokens: int
    output_tokens: int


class MockProvider:
    async def complete(self, spec, messages, output_tokens, timeout):
        await asyncio.sleep(0.01)
        observations = json.loads(messages[-1]["content"])["observations"]
        seen = {(o["request"]["account"], o["request"]["path"]) for o in observations}
        probes = [("A", "/orders/101"), ("B", "/orders/202"),
                  ("A", "/orders/202"), ("B", "/orders/101"), ("anonymous", "/orders/101")]
        if spec.id.endswith("b"):
            probes = [probes[1], probes[0], probes[3], probes[2], probes[4]]
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
    return isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {
        "RateLimitError", "ServiceUnavailableError", "APIConnectionError", "Timeout", "InternalServerError"
    }
