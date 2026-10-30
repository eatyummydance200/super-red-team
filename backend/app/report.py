import json
from collections import defaultdict
from copy import deepcopy
from itertools import combinations

from .catalog import execution_payload


def known_sum(values):
    values = list(values)
    return sum(values) if all(v is not None for v in values) else None


def counts(attempts):
    reproduced = sum(a.reproduced is True and not a.error for a in attempts)
    denied = sum(a.reproduced is False and not a.error for a in attempts)
    errors = sum(bool(a.error) for a in attempts)
    return {'attempts': len(attempts), 'reproduced': reproduced, 'not_reproduced': denied,
            'errors': errors, 'inconclusive_or_baseline': len(attempts) - reproduced - denied - errors,
            'reproduction_success_rate': reproduced / (reproduced + denied) if reproduced + denied else None}


def finding_key(attempt):
    evaluation = attempt.evaluation or {}
    if evaluation.get('finding_key'):
        return evaluation['finding_key']
    if evaluation.get('check') in {'cross_account', 'authentication'}:
        return 'orders:' + evaluation['check']
    return None


def coverage_complete(agent):
    return (agent.status == 'completed' and agent.termination_reason == 'model_stop'
            and all(c.progress == 'completed' and c.verdict not in {None, 'needs_review'} for c in agent.checks))


def report(execution):
    web = execution.config.scenario == 'web-access-v1'
    agents = execution.agents
    attempts = [a for agent in agents for a in agent.attempts]
    by_method = defaultdict(list)
    for attempt in attempts:
        by_method[attempt.method].append(attempt)
    methods = {key: counts(value) for key, value in by_method.items()}
    merged = {}
    for agent in agents:
        for finding in agent.findings:
            if finding.key not in merged:
                merged[finding.key] = deepcopy(finding.model_dump())
            else:
                current = merged[finding.key]
                for field in ('agent_run_ids', 'attempt_ids', 'evidence_ids'):
                    current[field] = list(dict.fromkeys(current[field] + getattr(finding, field)))
                for field in ('cvss', 'severity'):
                    if current[field] != getattr(finding, field):
                        current[field] = None
                priority = {'false_positive': 0, 'needs_review': 1, 'confirmed': 2}
                if priority[finding.verdict] > priority[current['verdict']]:
                    current['verdict'] = finding.verdict
    for key, finding in merged.items():
        related = [a for a in attempts if finding_key(a) == key]
        finding['reproduce_rate'] = counts(related)['reproduction_success_rate']
        finding['found_by'] = [a.model_name or a.model for a in agents if a.id in finding['agent_run_ids']]
        finding['target'] = next((a.request.path for a in related), key)
        finding['reproduction'] = list(dict.fromkeys(f'{a.request.account}: {a.request.method} {a.request.path}' for a in related if a.reproduced is True and not a.error))

    sets = {a.id: {f.key for f in a.findings if f.verdict == 'confirmed'} for a in agents}
    shared = {key for key in merged if sum(key in keys for keys in sets.values()) > 1}
    common = set.intersection(*sets.values()) if sets else set()
    costs = known_sum(a.estimated_cost for a in agents)
    tokens = sum(a.token_usage for a in agents) if all(a.token_usage_complete for a in agents) else None
    model_comparison, method_results = [], []
    for agent in agents:
        other = set().union(*(keys for agent_id, keys in sets.items() if agent_id != agent.id))
        model_comparison.append({
            'agent_id': agent.id, 'model': agent.model, 'model_name': agent.model_name or agent.model,
            'only_findings': sorted(sets[agent.id] - other), 'common_findings': sorted(sets[agent.id] & other),
            'only_count': len(sets[agent.id] - other), 'common_count': len(sets[agent.id] & other),
            'false_positive_count': sum(f.verdict == 'false_positive' for f in agent.findings),
            'duration_ms': agent.duration * 1000 if agent.duration is not None else None,
            'tokens': agent.token_usage if agent.token_usage_complete else None,
            'cost_usd': agent.estimated_cost, 'detection_rate': None,
        })
        for method in dict.fromkeys(a.method for a in agent.attempts):
            group = [a for a in agent.attempts if a.method == method]
            duration = known_sum(a.duration + a.model_duration if a.duration is not None and a.model_duration is not None else None for a in group)
            method_results.append({
                'method_id': method, 'method_name': method.replace('_', ' '),
                'target': ', '.join(dict.fromkeys(a.request.path for a in group)),
                'agent_id': agent.id, 'model_name': agent.model_name or agent.model,
                **counts(group), 'duration_ms': duration * 1000 if duration is not None else None,
                'tokens': known_sum(a.token_usage for a in group),
                'cost_usd': known_sum(a.estimated_cost for a in group),
            })

    ranked = []
    for size in range(1, len(agents) + 1):
        for subset in combinations(agents, size):
            keys = set().union(*(sets[a.id] for a in subset))
            values = []
            for key in keys:
                stats = counts([t for a in subset for t in a.attempts if finding_key(t) == key])
                cvss, rate = merged[key]['cvss'], stats['reproduction_success_rate']
                values.append(cvss * rate if cvss is not None and rate is not None else None)
            complete = all(coverage_complete(a) for a in subset)
            ranked.append({
                'combination_id': 'independent:' + '+'.join(a.model for a in subset),
                'models': [a.model_name or a.model for a in subset], 'model_ids': [a.model for a in subset],
                'collaboration_mode': 'independent', 'score': known_sum(values) if complete else None,
                'finding_count': len(keys), 'is_current': len(subset) == len(agents),
                'complete': complete, 'execution_id': execution.id,
            })

    checklist = []
    for check_id in dict.fromkeys(c.id for a in agents for c in a.checks):
        checks = [(a, c) for a in agents for c in a.checks if c.id == check_id]
        verdicts = [c.verdict for _, c in checks]
        status = ('vulnerable' if 'vulnerable' in verdicts else
                  'not_run' if all(c.progress == 'not_run' for _, c in checks) else
                  'needs_review' if len(checks) != len(agents) or any(c.progress == 'not_run' or c.verdict in {None, 'needs_review'} for _, c in checks) else
                  'not_applicable' if all(v == 'not_applicable' for v in verdicts) else 'passed')
        checklist.append({'id': check_id, 'group': 'access-control', 'title': check_id.replace('_', ' '),
                          'status': status, 'reason': '; '.join(dict.fromkeys(c.reason for _, c in checks)),
                          'agents': [{'agent_id': a.id, **c.model_dump()} for a, c in checks]})

    scope = {'scenario': execution.config.scenario,
             'target': execution.config.target_id if web else 'registered in-process local orders fixture',
             'target_url': execution.config.target_url,
             'environment_version': execution.config.target_id if web else execution.config.environment + '-v1',
             'state_changes': False,
             'isolation': 'Separate sessions; shared target data; login POST allowed' if web else 'separate ASGI application and data per agent; no runner network sockets'}
    comparison = {'merged_count': sum(f['verdict'] == 'confirmed' for f in merged.values()),
                  'common_count': len(shared), 'common': sorted(common), 'agents': model_comparison,
                  'complete': all(coverage_complete(a) for a in agents)}
    # Keep the old fields when reading previously saved two-model executions.
    if len(agents) == 2:
        first, second = agents
        comparison.update(model_a=first.model, model_b=second.model,
                          a_only=sorted(sets[first.id] - sets[second.id]), b_only=sorted(sets[second.id] - sets[first.id]))
    summary = {**counts(attempts), 'confirmed_findings': sum(f['verdict'] == 'confirmed' for f in merged.values()),
               'needs_review_findings': sum(f['verdict'] == 'needs_review' for f in merged.values()),
               'false_positive_findings': sum(f['verdict'] == 'false_positive' for f in merged.values()),
               'duration_ms': execution.duration * 1000 if execution.duration is not None else None,
               'tokens': tokens, 'cost_usd': costs}
    evidence, flattened_attempts = [], []
    for agent in agents:
        observations = {e.id: e.observation for e in agent.evidence}
        for attempt in agent.attempts:
            flattened_attempts.append({**attempt.model_dump(), 'agent_id': agent.id, 'method_id': attempt.method,
                'target': attempt.request.path, 'actor': attempt.request.account,
                'status_code': observations.get(attempt.evidence_id, {}).get('status_code')})
        for item in agent.evidence:
            evidence.append({**item.model_dump(), 'attempt_id': next((a.id for a in agent.attempts if a.evidence_id == item.id), None),
                'captured_at': item.created_at, 'request': json.dumps(item.observation.get('request'), ensure_ascii=False),
                'response': json.dumps({'status_code': item.observation.get('status_code'), 'body': item.observation.get('body')}, ensure_ascii=False)})
    return {
        'execution_id': execution.id, 'status': execution.status, 'partial_failure': execution.partial_failure,
        'is_final': execution.status not in {'pending', 'running'}, 'is_mock': execution.config.mode == 'mock',
        'partial': not comparison['complete'], 'evaluator': 'registered ownership oracle',
        'generated_at': execution.ended_at or (execution.events[-1]['timestamp'] if execution.events else execution.created_at),
        'result_kind': execution.config.mode + ('_models_with_registered_web_http' if web else '_simulation_with_local_http' if execution.config.mode == 'mock' else '_models_with_local_http'),
        'scope': scope, 'config': execution.config.model_dump(),
        'settings': {'scenario_name': execution.scenario_name or ('Registered website access control' if web else 'Order access control'),
                     'target_url': execution.config.target_url,
                     'environment': execution.config.environment, 'version': scope['environment_version'],
                     'actors': ['A', 'B', 'anonymous'], 'allowed_paths': execution.allowed_paths or (['/orders/{numeric_id}'] if not web else []),
                     'info_level': execution.config.information, 'category': 'access-control', 'tools': execution.config.tools,
                     'collaboration_mode': execution.config.collaboration,
                     'agents': [{'model_name': a.model_name or a.model, 'role': a.role, 'prompt_version': a.prompt_version} for a in agents]},
        'started_at': execution.started_at, 'ended_at': execution.ended_at, 'duration': execution.duration,
        'token_usage': tokens, 'token_usage_complete': all(a.token_usage_complete for a in agents),
        'estimated_cost': costs, 'cost_reasons': list(dict.fromkeys(a.cost_reason for a in agents if a.cost_reason)),
        'methods': methods, 'method_results': method_results, 'summary': summary,
        'findings': list(merged.values()), 'unique_vulnerability_count': summary['confirmed_findings'],
        'comparison': comparison, 'combinations': ranked, 'checklist': checklist,
        'agents': execution_payload(execution)['agents'], 'attempts': flattened_attempts, 'evidence': evidence,
        'detection_metrics': None, 'detection_metrics_reason': 'No complete benchmark labels; ownership observations alone cannot establish detection accuracy',
        'revalidation': {'supported': 'fresh execution against fixed target' if web else 'fresh execution with environment=fixed', 'linked_before_after_comparison': False},
        'limitations': ['Non-reproduction does not guarantee safety.',
                        'Missing CVSS, pricing, usage and scores remain null; no automatic CVSS assignment.',
                        'Combination ranking covers subsets of this execution only; it is not a cross-run benchmark.',
                        'Method usage covers calls that selected a test; stop calls and pre-test failures remain unallocated overhead.',
                        'Mock decisions are deterministic; registered web targets still receive real HTTP requests.',
                        'Cancellation does not guarantee cancellation of provider billing.',
                        'SQL injection, XSS and general vulnerability scanning are outside this scenario.'],
    }
