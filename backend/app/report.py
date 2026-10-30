from copy import deepcopy


def report(execution):
    merged = {}
    methods = {}
    for agent in execution.agents:
        for attempt in agent.attempts:
            counts = methods.setdefault(attempt.method, {"attempts": 0, "reproduced": 0, "not_reproduced": 0,
                                                        "errors": 0, "inconclusive_or_baseline": 0})
            counts["attempts"] += 1
            if attempt.error:
                counts["errors"] += 1
            elif attempt.reproduced is True:
                counts["reproduced"] += 1
            elif attempt.reproduced is False:
                counts["not_reproduced"] += 1
            else:
                counts["inconclusive_or_baseline"] += 1
        for finding in agent.findings:
            if finding.key not in merged:
                merged[finding.key] = deepcopy(finding.model_dump())
            else:
                for field in ("agent_run_ids", "attempt_ids", "evidence_ids"):
                    merged[finding.key][field] = list(dict.fromkeys(merged[finding.key][field] + getattr(finding, field)))
    for counts in methods.values():
        denominator = counts["reproduced"] + counts["not_reproduced"]
        counts["reproduction_success_rate"] = counts["reproduced"] / denominator if denominator else None
    first, second = execution.agents
    a, b = {f.key for f in first.findings}, {f.key for f in second.findings}
    costs = [agent.estimated_cost for agent in execution.agents]
    return {
        "execution_id": execution.id, "status": execution.status, "partial_failure": execution.partial_failure,
        "is_final": execution.status not in {"pending", "running"},
        "result_kind": "mock_simulation_with_local_http" if execution.config.mode == "mock" else "live_models_with_local_http",
        "scope": {"scenario": "orders-v1", "target": "registered in-process local orders fixture",
                  "environment_version": execution.config.environment + "-v1", "state_changes": False,
                  "isolation": "separate ASGI application and data per agent; no runner network sockets"},
        "config": execution.config.model_dump(), "started_at": execution.started_at, "ended_at": execution.ended_at,
        "duration": execution.duration, "token_usage": sum(a.token_usage for a in execution.agents),
        "token_usage_complete": all(a.token_usage_complete for a in execution.agents),
        "estimated_cost": sum(costs) if all(c is not None for c in costs) else None,
        "cost_reasons": list(dict.fromkeys(a.cost_reason for a in execution.agents if a.cost_reason)),
        "methods": methods, "findings": list(merged.values()), "unique_vulnerability_count": len(merged),
        "comparison": {"model_a": first.model, "model_b": second.model,
                       "a_only": sorted(a - b), "b_only": sorted(b - a), "common": sorted(a & b),
                       "complete": all(x.status == "completed" and x.termination_reason == "model_stop" for x in execution.agents)},
        "agents": [a.model_dump() for a in execution.agents],
        "detection_metrics": None,
        "detection_metrics_reason": "Fixture oracle validates individual observations; no complete benchmark label universe is defined",
        "revalidation": {"supported": "fresh execution with environment=fixed", "linked_before_after_comparison": False},
        "limitations": ["Non-reproduction does not guarantee safety.",
                        "Reproduction success rate is not vulnerability detection accuracy.",
                        "Mock model decisions are deterministic simulations, not real model exploration.",
                        "Cancellation stops local awaits; provider billing cancellation is not guaranteed."]
    }
