import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from .contracts import AgentResult, CheckResult, Decision, Evidence, Execution, ExecutionRequest, TestAttempt
from .evaluator import apply_evaluation, evaluate
from .fixture import create_fixture
from .gateway import ToolGateway
from .prompts import SYSTEM_V1, WEB_SYSTEM_V1
from .targets import WebGateway, evaluate_web, normalize_target_url
from .providers import LiteLLMProvider, MockProvider, parse_decision, transient
from .catalog import scenarios


def now():
    return datetime.now(timezone.utc).isoformat()


class BudgetExhausted(Exception):
    pass


class Manager:
    def __init__(self, store, models, provider=None, evaluator=evaluate, call_timeout=15, targets=None):
        self.store, self.models = store, models
        self.provider, self.evaluator, self.call_timeout = provider, evaluator, call_timeout
        self.tasks: dict[str, asyncio.Task] = {}
        self.active: dict[str, Execution] = {}
        self.stopping = False
        self.targets = targets or {}

    def event(self, execution, agent, kind, **data):
        messages = {
            "execution_started": "Execution started", "execution_finished": "Execution finished",
            "agent_started": "Independent agent started", "agent_finished": "Agent finished",
            "model_started": "Requesting the next test from the model", "model_completed": "Model response received",
            "model_error": "Model call failed", "model_interrupted": "Model call interrupted",
            "decision": "Model selected its next action", "tool_started": "Sending an allowed HTTP request",
            "tool_finished": "HTTP request and evidence processing finished", "cancel_requested": "Cancellation requested",
            "cancellation_processed": "Local execution cancelled; provider billing may continue",
            "recovered_as_failed": "Interrupted execution marked failed after restart",
        }
        if agent:
            phase = {"agent_started": "planning", "model_started": "thinking", "model_completed": "planning",
                     "tool_started": "testing", "tool_finished": "evaluating", "agent_finished": agent.status,
                     "cancellation_processed": agent.status}.get(kind)
            if phase:
                agent.phase = phase
            agent.progress = sum(c.progress == "completed" for c in agent.checks) / len(agent.checks) if agent.checks else 0
        execution.events.append({"sequence": len(execution.events) + 1, "timestamp": now(),
                                 "agent_run_id": agent.id if agent else None, "type": kind,
                                 "message": messages.get(kind, kind.replace("_", " ")),
                                 "level": "error" if data.get("error") or kind in {"model_error", "recovered_as_failed"} else "warn" if "cancel" in kind or "interrupted" in kind else "info", **data})
        self.store.save(execution)

    def recover(self):
        for execution in self.store.unfinished():
            execution.status, execution.error, execution.ended_at = "failed", "server_restart", now()
            if execution.started_at:
                execution.duration = (datetime.fromisoformat(execution.ended_at) - datetime.fromisoformat(execution.started_at)).total_seconds()
            for agent in execution.agents:
                if agent.status in {"pending", "running"}:
                    agent.status, agent.error, agent.termination_reason = "failed", "server_restart", "server_restart"
                    agent.phase = "failed"
                    agent.ended_at = execution.ended_at
                    if agent.started_at:
                        agent.duration = (datetime.fromisoformat(agent.ended_at) - datetime.fromisoformat(agent.started_at)).total_seconds()
                    for attempt in agent.attempts:
                        if attempt.ended_at is None:
                            attempt.ended_at, attempt.error = now(), "server_restart"
                    for call in agent.model_attempts:
                        if call["ended_at"] is None:
                            call.update(ended_at=now(), error="server_restart")
                            agent.token_usage_complete = False
                            agent.estimated_cost, agent.cost_reason = None, "Interrupted call may have been billed"
            execution.partial_failure = any(a.status == "completed" for a in execution.agents)
            self.event(execution, None, "recovered_as_failed", reason="No automatic replay")

    def create(self, config: ExecutionRequest):
        if self.stopping or self.active:
            raise RuntimeError("Only one execution at a time is supported")
        if config.target_url is not None:
            url = normalize_target_url(config.target_url)
            target_id = next((key for key, target in self.targets.items()
                              if url in {normalize_target_url(target.base_url), *target.urls}), None)
            if target_id is None:
                raise ValueError("Target URL is not registered by the backend operator")
            if config.target_id is not None and config.target_id != target_id:
                raise ValueError("target_id and target_url refer to different sites")
            config = config.model_copy(update={"target_id": target_id, "target_url": url})
        target = self.targets.get(config.target_id) if config.target_id else None
        if config.target_id and target is None:
            raise ValueError("Target is not registered by the backend operator")
        if target:
            config = config.model_copy(update={"environment": target.environment})
            import os
            if any(not os.getenv(name) for login in target.accounts.values() for name in login.fields_env.values()):
                raise ValueError("Missing target credential environment variable")
        for agent in config.agents:
            spec = self.models.get(agent.model)
            if spec is None or spec.mode != config.mode:
                raise ValueError("Select two registered models matching the execution mode")
            if config.budget.cost_usd is not None and spec.reservation_cost(1, 1) is None:
                raise ValueError("Cost cap requires server-configured input/output rates for both models")
            if config.mode == "live":
                import os
                if not os.getenv(spec.key_env):
                    raise ValueError(f"Missing backend environment variable: {spec.key_env}")
        execution_id = uuid4().hex
        execution = Execution(id=execution_id, config=config, created_at=now(), agents=[
            AgentResult(id=uuid4().hex, execution_id=execution_id, model=a.model, role=a.role,
                        model_name=self.models[a.model].name or a.model,
                        prompt_version=a.prompt_version, instructions=a.instructions,
                        environment_version=config.environment + "-v1")
            for a in config.agents
        ])
        metadata = next(s for s in scenarios(self.targets) if s["id"] == config.scenario)
        execution.scenario_name = metadata["name"] + (" · " + (target.name or config.target_id) if target else "")
        execution.allowed_paths = list(target.allowed_paths if target else metadata["allowed_paths"])
        if target:
            for agent in execution.agents:
                agent.scenario_version, agent.environment_version = "web-access-v1", config.target_id
                agent.checks = [CheckResult(id=f"web_access:{account}:{r.path}")
                                for r in target.resources for account in (r.owner, "B" if r.owner == "A" else "A", "anonymous")]
        self.store.save(execution)
        self.active[execution.id] = execution
        self.tasks[execution.id] = asyncio.create_task(self.run(execution))
        return execution

    async def model_call(self, execution, agent, messages):
        budget, spec = execution.config.budget, self.models[agent.model]
        provider = self.provider or (MockProvider() if spec.mode == "mock" else LiteLLMProvider())
        # shortcut: reserve UTF-8 bytes plus framing as a conservative token bound;
        # use provider-specific exact accounting before supporting arbitrary tools/messages.
        input_bound = sum(len(m["content"].encode("utf-8")) + 256 for m in messages) + 256
        reserved_tokens = input_bound + budget.max_output_tokens
        reserved_cost = spec.reservation_cost(input_bound, budget.max_output_tokens)
        for retry in range(2):
            if agent.model_calls >= budget.model_calls:
                raise BudgetExhausted("model_calls")
            if agent.token_budget_consumed + reserved_tokens > budget.tokens:
                raise BudgetExhausted("tokens")
            if budget.cost_usd is not None and agent.cost_budget_consumed + reserved_cost > budget.cost_usd:
                raise BudgetExhausted("cost")
            agent.model_calls += 1
            agent.token_budget_consumed += reserved_tokens
            agent.cost_budget_consumed += reserved_cost or 0
            record = {"started_at": now(), "ended_at": None, "retry": retry,
                      "reserved_tokens": reserved_tokens, "input_tokens": None, "output_tokens": None,
                      "estimated_cost": None, "error": None}
            agent.model_attempts.append(record)
            self.event(execution, agent, "model_started", retry=retry)
            try:
                async with asyncio.timeout(self.call_timeout):
                    reply = await provider.complete(spec, messages, budget.max_output_tokens, self.call_timeout)
            except BaseException as error:
                record.update(ended_at=now(), error=type(error).__name__)
                agent.token_usage_complete = False
                agent.estimated_cost, agent.cost_reason = None, "Failed or cancelled call may be billed; usage unavailable"
                self.event(execution, agent, "model_interrupted" if isinstance(error, asyncio.CancelledError) else "model_error",
                           error=type(error).__name__, provider_billing_cancelled=False)
                if isinstance(error, Exception) and transient(error) and retry == 0:
                    await asyncio.sleep(0.05)
                    continue
                raise
            else:
                actual = reply.input_tokens + reply.output_tokens
                cost = spec.reservation_cost(reply.input_tokens, reply.output_tokens)
                record.update(ended_at=now(), input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                              estimated_cost=cost)
                agent.token_usage += actual
                agent.token_budget_consumed += actual - reserved_tokens
                if reserved_cost is not None and cost is not None:
                    agent.cost_budget_consumed += cost - reserved_cost
                if cost is None:
                    agent.estimated_cost, agent.cost_reason = None, "Server pricing not configured"
                elif agent.estimated_cost is not None:
                    agent.estimated_cost += cost
                self.event(execution, agent, "model_completed")
                if actual > reserved_tokens or reply.output_tokens > budget.max_output_tokens:
                    raise ValueError("Provider exceeded reserved token bound")
                return parse_decision(reply.content)

    async def agent_loop(self, execution, agent):
        start = time.monotonic()
        agent.status, agent.started_at = "running", now()
        target = self.targets.get(execution.config.target_id)
        gateway = WebGateway(target) if target else ToolGateway(create_fixture(execution.config.environment))
        observations = []
        self.event(execution, agent, "agent_started")
        try:
            async with asyncio.timeout(execution.config.budget.seconds):
                while True:
                    messages = [
                        {"role": "system", "content": (WEB_SYSTEM_V1 if target else SYSTEM_V1).format(role=agent.role, schema=json.dumps(Decision.model_json_schema()))},
                        {"role": "user", "content": json.dumps({"observations": observations,
                         "instructions": agent.instructions,
                         "prior_claims": agent.claims, "requests_remaining": execution.config.budget.requests - agent.requests})}
                    ]
                    if target:
                        context = json.loads(messages[-1]["content"])
                        context["target"] = {"allowed_paths": target.allowed_paths,
                                             "resources": [{"path": r.path, "owner": r.owner} for r in target.resources]}
                        messages[-1]["content"] = json.dumps(context)
                    first_call = len(agent.model_attempts)
                    decision_start = time.monotonic()
                    decision = await self.model_call(execution, agent, messages)
                    decision_duration = time.monotonic() - decision_start
                    calls = agent.model_attempts[first_call:]
                    agent.claims.append({"timestamp": now(), **decision.model_dump()})
                    self.event(execution, agent, "decision", action=decision.action)
                    if decision.action == "stop":
                        agent.termination_reason = "model_stop"
                        break
                    if agent.requests >= execution.config.budget.requests:
                        raise BudgetExhausted("requests")
                    agent.requests += 1
                    attempt = TestAttempt(id=uuid4().hex, execution_id=execution.id, agent_run_id=agent.id,
                                          hypothesis=decision.hypothesis, method="web_access" if target else "http_order_access",
                                          request=decision.request, started_at=now(), model_duration=decision_duration,
                                          token_usage=sum(c["input_tokens"] + c["output_tokens"] for c in calls) if all(c["input_tokens"] is not None and c["output_tokens"] is not None for c in calls) else None,
                                          estimated_cost=sum(c["estimated_cost"] for c in calls) if all(c["estimated_cost"] is not None for c in calls) else None)
                    agent.attempts.append(attempt)
                    self.event(execution, agent, "tool_started", attempt_id=attempt.id)
                    attempt_start = time.monotonic()
                    try:
                        async with asyncio.timeout(5):
                            observation = await gateway.execute(decision.request)
                        evidence_id = uuid4().hex
                        artifact, digest = self.store.artifact(execution.id, agent.id, evidence_id, observation)
                        agent.evidence.append(Evidence(id=evidence_id, execution_id=execution.id, agent_run_id=agent.id,
                                                       created_at=now(), sha256=digest, artifact=artifact, observation=observation))
                        attempt.evidence_id = evidence_id
                        self.store.save(execution)
                        # Only raw observations return to this agent, never oracle judgments or peer state.
                        observations.append(observation)
                        evaluation = evaluate_web(target, attempt, observation, observations[:-1]) if target else self.evaluator(attempt, observation)
                        apply_evaluation(agent, attempt, evaluation)
                    except BaseException as error:
                        attempt.error = type(error).__name__
                        raise
                    finally:
                        attempt.ended_at, attempt.duration = now(), time.monotonic() - attempt_start
                        self.event(execution, agent, "tool_finished", attempt_id=attempt.id, error=attempt.error)
                agent.status = "completed"
        except BudgetExhausted as error:
            agent.status, agent.termination_reason = "completed", f"budget_exhausted:{error}"
        except asyncio.CancelledError:
            reason = "server_shutdown" if self.stopping else "user_cancelled"
            agent.status, agent.termination_reason = ("failed" if self.stopping else "cancelled"), reason
            agent.error = reason
            self.event(execution, agent, "cancellation_processed", new_work_stopped=True,
                       local_await_cancelled=True, provider_billing_cancelled=False)
            raise
        except Exception as error:
            agent.status, agent.error, agent.termination_reason = "failed", type(error).__name__, "error"
        finally:
            agent.ended_at, agent.duration = now(), time.monotonic() - start
            for check in agent.checks:
                if check.progress == "not_run":
                    check.reason = agent.termination_reason or "Agent did not perform this check"
            self.event(execution, agent, "agent_finished", status=agent.status, reason=agent.termination_reason)
            await gateway.close()

    async def run(self, execution):
        start = time.monotonic()
        execution.started_at, execution.status = now(), "running"
        self.event(execution, None, "execution_started")
        try:
            await asyncio.gather(*(self.agent_loop(execution, a) for a in execution.agents))
            failed = [a for a in execution.agents if a.status == "failed"]
            execution.partial_failure = 0 < len(failed) < len(execution.agents)
            execution.status = "failed" if failed else "completed"
        except asyncio.CancelledError:
            execution.status = "failed" if self.stopping else "cancelled"
            execution.error = "server_shutdown" if self.stopping else "user_cancelled"
            execution.partial_failure = any(a.status == "completed" for a in execution.agents)
        finally:
            execution.ended_at, execution.duration = now(), time.monotonic() - start
            self.event(execution, None, "execution_finished", status=execution.status)
            self.active.pop(execution.id, None)
            self.tasks.pop(execution.id, None)

    async def cancel(self, execution_id):
        execution = self.active.get(execution_id)
        if execution is None:
            return self.store.get(execution_id)
        task = self.tasks[execution_id]
        self.event(execution, None, "cancel_requested")
        if execution.status == "pending":
            status, reason = ("failed", "server_shutdown") if self.stopping else ("cancelled", "cancelled_before_start")
            execution.status, execution.error, execution.ended_at = status, reason, now()
            for agent in execution.agents:
                agent.status, agent.termination_reason, agent.ended_at = status, reason, now()
                agent.phase = status
            self.store.save(execution)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.active.pop(execution_id, None)
        self.tasks.pop(execution_id, None)
        return self.store.get(execution_id)

    async def close(self):
        self.stopping = True
        for execution_id in list(self.tasks):
            await self.cancel(execution_id)
