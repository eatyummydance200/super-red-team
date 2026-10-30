"""Public display metadata; never includes target credentials or ownership markers."""
def scenarios(targets):
    result = [{
        "id": "orders-v1", "name": "Order access control", "description": "Compare owner, cross-account and anonymous order reads in an isolated fixture.",
        "service": "orders", "environments": ["vulnerable", "fixed"],
        "environment_versions": {"vulnerable": "vulnerable-v1", "fixed": "fixed-v1"},
        "allowed_paths": ["/orders/{numeric_id}"], "accounts": ["A", "B"],
        "information": "gray-box", "tools": ["http"], "state_changes": False,
        "collaboration": "independent", "prompt_versions": ["v1"],
        "roles": ["access-control tester"],
        "supported_checks": ["own_order_A", "own_order_B", "cross_account", "authentication"],
        "unsupported": ["external_url", "sequential", "debate", "browser", "code_reading", "writes", "scheduling"],
    }]
    result.append({
        "id": "web-access-v1", "name": "Registered website access control",
        "description": "Read only registered resource paths using isolated account sessions.",
        "service": "web", "target_ids": list(targets), "environments": ["vulnerable"],
        "environment_versions": {"vulnerable": "operator-registered-target"},
        "targets": [{"id": key, "allowed_paths": value.allowed_paths} for key, value in targets.items()],
        "allowed_paths": [], "accounts": ["A", "B"], "information": "gray-box",
        "tools": ["http"], "state_changes": False, "collaboration": "independent",
        "prompt_versions": ["v1"], "roles": ["access-control tester"],
        "supported_checks": ["owner_baseline", "cross_account", "authentication"],
        "limitations": ["Operator-registered loopback targets only", "Login POST is the only write"],
    })
    return result


def execution_payload(execution):
    data = execution.model_dump()
    data["is_mock"] = execution.config.mode == "mock"
    data["scenario_name"] = execution.scenario_name or ("Order access control" if execution.config.scenario == "orders-v1" else "Registered website access control")
    for agent in data["agents"]:
        agent["is_mock"] = data["is_mock"]
        agent["token_usage"] = agent["token_usage"] if agent["token_usage_complete"] else None
        checks = agent["checks"]
        agent["progress"] = sum(c["progress"] == "completed" for c in checks) / len(checks) if checks else 0
        if agent["status"] in {"completed", "failed", "cancelled"}:
            agent["phase"] = agent["status"]
    return data
