from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(Contract):
    seconds: float = Field(default=60, ge=0.05, le=300)
    requests: int = Field(default=12, ge=1, le=50)
    model_calls: int = Field(default=12, ge=1, le=30)
    tokens: int = Field(default=100_000, ge=100, le=1_000_000)
    max_output_tokens: int = Field(default=1000, ge=100, le=4000)
    cost_usd: float | None = Field(default=None, gt=0, le=10)


class AgentConfig(Contract):
    model: str = Field(min_length=1, max_length=150)
    role: str = Field(default="access-control tester", min_length=1, max_length=300)
    prompt_version: Literal["v1"] = "v1"
    instructions: str = Field(default="", max_length=1500)


class ExecutionRequest(Contract):
    mode: Literal["mock", "live"] = "mock"
    scenario: Literal["orders-v1"] = "orders-v1"
    environment: Literal["vulnerable", "fixed"] = "vulnerable"
    collaboration: Literal["independent"] = "independent"
    information: Literal["gray-box"] = "gray-box"
    tools: list[Literal["http"]] = Field(default_factory=lambda: ["http"], min_length=1, max_length=1)
    state_changes: Literal[False] = False
    agents: list[AgentConfig] = Field(default_factory=lambda: [
        AgentConfig(model="mock/explorer-a"), AgentConfig(model="mock/explorer-b")
    ], min_length=2, max_length=2)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def distinct_models(self):
        if self.agents[0].model == self.agents[1].model:
            raise ValueError("Two distinct registered models are required")
        return self


class ToolRequest(Contract):
    method: Literal["GET"] = "GET"
    path: str = Field(min_length=1, max_length=120)
    account: Literal["A", "B", "anonymous"]


class Decision(Contract):
    hypothesis: str = Field(min_length=1, max_length=1500)
    action: Literal["http", "stop"]
    request: ToolRequest | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if (self.action == "http") != (self.request is not None):
            raise ValueError("http needs request; stop must not have request")
        return self


class Evidence(Contract):
    id: str
    execution_id: str
    agent_run_id: str
    created_at: str
    sha256: str
    artifact: str
    observation: dict


class CheckResult(Contract):
    id: str
    progress: Literal["not_run", "completed"] = "not_run"
    verdict: Literal["vulnerable", "pass", "needs_review", "not_applicable"] | None = None
    reason: str = "No qualifying observation"
    attempt_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)


class TestAttempt(Contract):
    id: str
    execution_id: str
    agent_run_id: str
    hypothesis: str
    method: str
    request: ToolRequest
    started_at: str
    ended_at: str | None = None
    duration: float | None = None
    evidence_id: str | None = None
    reproduced: bool | None = None
    evaluation: dict | None = None
    error: str | None = None


class Finding(Contract):
    id: str
    key: str
    title: str
    impact: str
    remediation: str
    attempt_ids: list[str]
    evidence_ids: list[str]
    agent_run_ids: list[str]


class AgentResult(Contract):
    id: str
    execution_id: str
    model: str
    role: str
    prompt_version: str
    instructions: str = ""
    scenario_version: str = "orders-v1"
    environment_version: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    started_at: str | None = None
    ended_at: str | None = None
    duration: float | None = None
    termination_reason: str | None = None
    error: str | None = None
    model_calls: int = 0
    requests: int = 0
    token_usage: int = 0
    token_usage_complete: bool = True
    token_budget_consumed: int = 0
    estimated_cost: float | None = 0
    cost_reason: str | None = None
    cost_budget_consumed: float = 0
    claims: list[dict] = Field(default_factory=list)
    model_attempts: list[dict] = Field(default_factory=list)
    attempts: list[TestAttempt] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=lambda: [
        CheckResult(id="own_order_A"), CheckResult(id="own_order_B"),
        CheckResult(id="cross_account"), CheckResult(id="authentication")
    ])


class Execution(Contract):
    id: str
    config: ExecutionRequest
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    duration: float | None = None
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    partial_failure: bool = False
    error: str | None = None
    agents: list[AgentResult]
    events: list[dict] = Field(default_factory=list)
