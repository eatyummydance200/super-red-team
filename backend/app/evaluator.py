"""Evaluation-only ownership oracle. Never included in model context or runner config."""
from .contracts import Finding, TestAttempt

OWNERS = {"101": "A", "202": "B"}


def evaluate(attempt: TestAttempt, observation: dict) -> dict:
    request = attempt.request
    order_id = request.path.rsplit("/", 1)[-1]
    expected_owner = OWNERS.get(order_id)
    body, status = observation["body"], observation["status_code"]
    exposed = (isinstance(body, dict) and body.get("id") == order_id
               and body.get("owner") == expected_owner
               and isinstance(body.get("item"), str) and isinstance(body.get("total"), (int, float)))
    if expected_owner is None:
        return {"check": None, "verdict": "needs_review", "reproduced": None,
                "reason": "No evaluation oracle for this object"}
    if request.account == "anonymous":
        check = "authentication"
    elif request.account == expected_owner:
        return {"check": f"own_order_{request.account}", "verdict": "pass" if status == 200 and exposed else "needs_review",
                "reproduced": None, "reason": "Verify authorized data access against the fixture oracle"}
    else:
        check = "cross_account"
    reproduced = True if exposed and status == 200 else False if status in {401, 403, 404} and not exposed else None
    return {"check": check, "verdict": "vulnerable" if reproduced else "pass" if reproduced is False else "needs_review",
            "reproduced": reproduced, "reason": "Matched foreign order data" if reproduced else
            "Access denied without order data" if reproduced is False else "HTTP status alone is insufficient"}


def apply_evaluation(agent, attempt, evaluation):
    attempt.method = evaluation["check"] or "unclassified_http"
    attempt.evaluation = evaluation
    attempt.reproduced = evaluation["reproduced"]
    check = next((c for c in agent.checks if c.id == evaluation["check"]), None)
    if check is None:
        return
    check.progress = "completed"
    check.attempt_ids.append(attempt.id)
    priority = {None: -1, "pass": 0, "not_applicable": 0, "needs_review": 1, "vulnerable": 2}
    if priority[evaluation["verdict"]] >= priority[check.verdict]:
        check.verdict, check.reason = evaluation["verdict"], evaluation["reason"]
    if attempt.reproduced:
        key = evaluation.get("finding_key", f"orders:{check.id}")
        finding = next((f for f in agent.findings if f.key == key), None)
        if finding is None:
            web = key.startswith("web-access:")
            finding = Finding(id=attempt.id, key=key, title="Private web content exposed without authorization" if web else "Order data exposed without required authorization",
                              cvss=evaluation.get("cvss"), severity=evaluation.get("severity"),
                              impact="Another account's private content can be read." if web else "Another account's order contents can be read.",
                              remediation="Authenticate and enforce object ownership on every resource lookup.",
                              attempt_ids=[], evidence_ids=[], agent_run_ids=[agent.id])
            agent.findings.append(finding)
        finding.attempt_ids.append(attempt.id)
        finding.evidence_ids.append(attempt.evidence_id)
        if finding.id not in check.finding_ids:
            check.finding_ids.append(finding.id)
