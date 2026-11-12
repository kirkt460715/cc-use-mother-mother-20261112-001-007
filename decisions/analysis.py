from equipment_domain.values import digest, day, exact
from risk_model.scoring import score, verify as verify_score, priority_stability, inspection_value, value
from risk_model.portfolio import summary, investment_distribution
from funding.optimizer import optimize, verify as verify_plan
from funding.explanations import exclusions, objective_attribution, dominance
from equipment_domain.maintenance import operating_inventory, component_warnings, overlaps


def analyze(catalog, as_of, known_at, mode='central', maximum_nodes=500000):
    risk = score(catalog, as_of, known_at)
    plan = optimize(catalog, risk, mode, maximum_nodes=maximum_nodes)
    from equipment_domain.quality import inspect
    from funding.lots import schedule, daily_inventory, lot_summary
    calendar = schedule(catalog, risk, plan) if plan['status'] != 'infeasible' else None
    body = {
        'quality': inspect(catalog, risk),
        'candidate_calendar': calendar,
        'calendar_inventory': daily_inventory(catalog, calendar) if calendar is not None else None,
        'procurement_lots': lot_summary(catalog, plan, calendar) if calendar is not None else None,
        'version': 1,
        'input_digest': catalog.fingerprint,
        'as_of': as_of,
        'known_at': known_at,
        'risk': risk,
        'plan': plan,
        'risk_by_region': summary(risk),
        'risk_by_class': summary(risk, 'class'),
        'ranking_stability': priority_stability(risk),
        'inspection_priority': inspection_value(risk),
        'operating_inventory': operating_inventory(catalog, as_of),
        'component_warnings': component_warnings(catalog, as_of),
        'maintenance_overlaps': overlaps(catalog, known_at),
        'candidate_dominance': dominance(catalog, risk, mode),
        'exclusion_reasons': exclusions(catalog, risk, plan),
        'investment_by_region': investment_distribution(catalog, risk, plan) if plan['status'] != 'infeasible' else None,
        'objective_attribution': objective_attribution(catalog, risk, plan) if plan['status'] != 'infeasible' else None,
        'limitations': [
            'risk_index_not_failure_probability',
            'declared_risk_reductions_not_causal_estimates',
            'period_outages_assumed_simultaneous_conservatively',
            'no_cross_period_budget_carryover',
            'requires_separate_human_review_before_real_procurement',
        ],
    }
    return {**body, 'analysis_digest': digest(body)}


def verify(catalog, result, replay=False):
    if result['version'] != 1 or result['input_digest'] != catalog.fingerprint:
        raise ValueError('analysis source binding mismatch')
    body = {key: value for key, value in result.items() if key != 'analysis_digest'}
    if digest(body) != result['analysis_digest']:
        raise ValueError('analysis checksum mismatch')
    scored = verify_score(catalog, result['risk'])
    planned = verify_plan(catalog, result['risk'], result['plan'], replay=replay)
    if result['as_of'] != result['risk']['as_of'] or result['known_at'] != result['risk']['known_at']:
        raise ValueError('analysis dates disagree with risk context')
    if result['plan']['status'] != 'infeasible':
        investment = result['investment_by_region']
        cost = sum(row['cost_cents'] for row in investment['rows'])
        if cost != result['plan']['objective']['cost_cents']:
            raise ValueError('analysis investment money does not conserve')
    if result['candidate_calendar'] is not None:
        from funding.lots import audit as audit_schedule
        audit_schedule(catalog, result['risk'], result['plan'], result['candidate_calendar'])
    if replay:
        plan = result['plan']
        limit = plan.get('search', {}).get('maximum_nodes', plan.get('maximum_nodes', 500000))
        expected = analyze(catalog, result['as_of'], result['known_at'], plan['mode'], limit)
        if expected != result:
            raise ValueError('analysis replay mismatch')
    return {'valid': True, 'risk': scored, 'plan': planned, 'replayed': replay}


def compare(before, after):
    if before['as_of'] != after['as_of']:
        raise ValueError('decision comparison requires same business day')
    old = {row['asset']: row for row in before['risk']['rows']}
    new = {row['asset']: row for row in after['risk']['rows']}
    rows = []
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        if left == right:
            continue
        rows.append({
            'asset': key,
            'before_band': left['band'] if left else None,
            'after_band': right['band'] if right else None,
            'central_change': exact(value(right['central']) - value(left['central'])) if left and right else None,
            'before_review': left['requires_evidence_review'] if left else None,
            'after_review': right['requires_evidence_review'] if right else None,
        })
    first = set(before['plan']['selected'])
    last = set(after['plan']['selected'])
    return {'before': before['analysis_digest'], 'after': after['analysis_digest'], 'risk_changes': rows, 'added_proposals': sorted(last - first), 'removed_proposals': sorted(first - last), 'unchanged_selected': sorted(first & last)}
