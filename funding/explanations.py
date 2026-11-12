from fractions import Fraction
from equipment_domain.values import exact, amount
from funding.constraints import evaluate, closure, incompatibilities
from funding.optimizer import optimize, benefits
from risk_model.scoring import value


def exclusions(catalog, scored, plan):
    if plan['status'] == 'infeasible':
        return {'rows': [], 'reason': 'no_feasible_portfolio'}
    chosen = set(plan['selected'])
    output = []
    pairs = incompatibilities(catalog)
    for key in sorted(catalog.proposals):
        if key in chosen:
            continue
        candidate = closure(catalog, chosen | {key})
        check = evaluate(catalog, scored, candidate)
        conflicts = [list(pair) for pair in pairs if key in pair and set(pair) <= candidate]
        output.append({
            'proposal': key,
            'requires_additional': sorted(candidate - chosen - {key}),
            'add_to_existing_feasible': check['feasible'],
            'violations_if_added': check['violations'],
            'conflicts_with_current': conflicts,
            'explanation': 'constraint_blocks_direct_addition' if not check['feasible'] else 'tie_or_zero_marginal_value_under_objective',
            'does_not_claim_candidate_is_globally_dominated': True,
        })
    return {'rows': output, 'basis_plan': plan['plan_digest']}


def forced_tradeoffs(catalog, scored, plan, maximum_nodes=100000):
    if plan['status'] == 'infeasible':
        raise ValueError('tradeoffs require feasible reference plan')
    baseline = value(plan['objective']['exposure_reduction'])
    rows = []
    for key in sorted(catalog.proposals):
        if key in plan['selected']:
            continue
        variant = optimize(catalog, scored, plan['mode'], force=[key], maximum_nodes=maximum_nodes)
        feasible = variant['status'] != 'infeasible'
        rows.append({
            'proposal': key,
            'feasible_when_forced': feasible,
            'selected': variant['selected'],
            'removed': sorted(set(plan['selected']) - set(variant['selected'])) if feasible else [],
            'added': sorted(set(variant['selected']) - set(plan['selected'])) if feasible else [],
            'exposure_reduction_difference': exact(value(variant['objective']['exposure_reduction']) - baseline) if feasible else None,
            'optimality_proven': variant['optimality_proven'],
            'reason': variant.get('reason'),
        })
    return {'basis_plan': plan['plan_digest'], 'rows': rows, 'each_counterfactual_reoptimizes_other_candidates': True}


def dominance(catalog, scored, mode='central'):
    benefit = benefits(catalog, scored, mode)
    pairs = []
    values = sorted(catalog.proposals)
    for left in values:
        a = catalog.proposals[left]
        for right in values:
            if left == right:
                continue
            b = catalog.proposals[right]
            if a['asset'] != b['asset'] or a['period'] != b['period']:
                continue
            if set(a['requires']) != set(b['requires']) or set(a['excludes']) != set(b['excludes']):
                continue
            if a['mandatory'] or b['mandatory']:
                continue
            no_worse = amount(a['cost'], 'cost') <= amount(b['cost'], 'cost') and a['crew_days'] <= b['crew_days'] and a['outage_days'] <= b['outage_days'] and value(benefit[left]['exposure_reduction']) >= value(benefit[right]['exposure_reduction'])
            strict = amount(a['cost'], 'cost') < amount(b['cost'], 'cost') or a['crew_days'] < b['crew_days'] or a['outage_days'] < b['outage_days'] or value(benefit[left]['exposure_reduction']) > value(benefit[right]['exposure_reduction'])
            if no_worse and strict:
                pairs.append({'preferred': left, 'dominated': right, 'same_asset_and_period': True})
    return {'pairs': pairs, 'mode': mode, 'screening_only_no_candidates_silently_deleted': True}


def objective_attribution(catalog, scored, plan):
    if plan['status'] == 'infeasible':
        raise ValueError('attribution requires feasible plan')
    rows = {row['proposal']: row for row in plan['benefits']}
    total = value(plan['objective']['exposure_reduction'])
    output = []
    for key in plan['selected']:
        row = rows[key]
        reduction = value(row['exposure_reduction'])
        output.append({
            'proposal': key,
            'asset': row['asset'],
            'exposure_reduction': exact(reduction),
            'share': exact(reduction / total) if total else None,
            'cost_cents': row['cost_cents'],
            'cost_per_index_user': exact(Fraction(row['cost_cents'], 1) / reduction) if reduction else None,
            'evidence_review': row['evidence_review'],
        })
    if sum(value(row['exposure_reduction']) for row in output) != total:
        raise ValueError('objective attribution conservation failed')
    return {'rows': output, 'total': exact(total), 'cost_effectiveness_unit': 'cents_per_declared_index_user_not_cost_per_life_saved'}
