from fractions import Fraction
from equipment_domain.values import amount, ratio, exact, digest, integer
from funding.constraints import evaluate, closure, incompatibilities, dependency_order
from risk_model.scoring import value


def benefits(catalog, scored, mode='central'):
    if mode not in ('low', 'central', 'high'):
        raise ValueError('benefit mode must be low, central, or high')
    assets = {row['asset']: row for row in scored['rows']}
    result = {}
    for key, proposal in sorted(catalog.proposals.items()):
        risk = assets[proposal['asset']]
        reduction = ratio(proposal['reduction'], 'proposal.reduction')
        before = value(risk[mode])
        improvement = before * reduction
        asset = catalog.assets[proposal['asset']]
        outage = amount(asset['outage_cost_daily'], 'outage_cost_daily') * proposal['outage_days']
        result[key] = {
            'proposal': key,
            'asset': proposal['asset'],
            'mode': mode,
            'risk_before': exact(before),
            'risk_reduction': exact(improvement),
            'risk_after': exact(before - improvement),
            'exposure_reduction': exact(improvement * max(1, risk['users_exposed'])),
            'outage_cost_cents': outage,
            'cost_cents': amount(proposal['cost'], 'cost'),
            'evidence_review': risk['requires_evidence_review'],
            'reduction_is_declared_engineering_assumption_not_learned_probability': True,
        }
    return result


def objective(benefit_rows, selected):
    improvement = sum((value(benefit_rows[key]['exposure_reduction']) for key in selected), Fraction(0))
    cost = sum(benefit_rows[key]['cost_cents'] for key in selected)
    outage = sum(benefit_rows[key]['outage_cost_cents'] for key in selected)
    return improvement, -cost, -outage


def optimize(catalog, scored, mode='central', force=(), forbid=(), maximum_nodes=500000):
    integer(maximum_nodes, 'maximum_nodes', 1, 10_000_000)
    force = set(force)
    forbid = set(forbid)
    if (force | forbid) - catalog.proposals.keys():
        raise ValueError('unknown forced or forbidden proposal')
    if force & forbid:
        raise ValueError('proposal both forced and forbidden')
    required = {key for key, row in catalog.proposals.items() if row['mandatory']}
    required = closure(catalog, required | force)
    benefit = benefits(catalog, scored, mode)
    if required & forbid:
        return infeasible(scored, mode, benefit, 'mandatory_or_forced_dependency_forbidden', force=force, forbid=forbid, maximum_nodes=maximum_nodes)
    order = dependency_order(catalog)
    order = sorted(order, key=lambda key: (-value(benefit[key]['exposure_reduction']), key))
    candidates = [key for key in order if key not in required and key not in forbid]
    initial = evaluate(catalog, scored, required)
    if not initial['feasible']:
        return infeasible(scored, mode, benefit, 'mandatory_or_forced_set_infeasible', initial['violations'], force, forbid, maximum_nodes)
    best = set(required)
    best_objective = objective(benefit, best)
    best_key = tuple(sorted(best))
    nodes = 0
    pruned_bound = 0
    pruned_constraints = 0
    exhausted = True
    stack = [(0, frozenset(required), frozenset(forbid))]
    while stack:
        if nodes >= maximum_nodes:
            exhausted = False
            break
        position, selected, rejected = stack.pop()
        nodes += 1
        current = objective(benefit, selected)
        key = tuple(sorted(selected))
        if current > best_objective or (current == best_objective and key < best_key):
            best = set(selected)
            best_objective = current
            best_key = key
        if position >= len(candidates):
            continue
        remaining = set(candidates[position:]) - selected - rejected
        optimistic = current[0] + sum((value(benefit[key]['exposure_reduction']) for key in remaining), Fraction(0))
        if optimistic < best_objective[0]:
            pruned_bound += 1
            continue
        candidate = candidates[position]
        if candidate in selected or candidate in rejected:
            stack.append((position + 1, selected, rejected))
            continue
        stack.append((position + 1, selected, rejected | {candidate}))
        included = closure(catalog, set(selected) | {candidate})
        if included & rejected:
            pruned_constraints += 1
            continue
        check = evaluate(catalog, scored, included)
        if not check['feasible']:
            pruned_constraints += 1
            continue
        stack.append((position + 1, frozenset(included), rejected))
    checked = evaluate(catalog, scored, best)
    body = {
        'score_digest': scored['score_digest'],
        'mode': mode,
        'status': 'optimal' if exhausted else 'feasible_search_limit',
        'optimality_proven': exhausted,
        'selected': sorted(best),
        'objective': {'exposure_reduction': exact(best_objective[0]), 'cost_cents': -best_objective[1], 'outage_cost_cents': -best_objective[2]},
        'constraint_check': checked,
        'benefits': [benefit[key] for key in sorted(benefit)],
        'search': {'visited_nodes': nodes, 'bound_prunes': pruned_bound, 'constraint_prunes': pruned_constraints, 'maximum_nodes': maximum_nodes},
        'force': sorted(force),
        'forbid': sorted(forbid),
        'tie_break': 'max_exposure_reduction_min_cost_min_outage_lexicographic_selected_ids',
    }
    return {**body, 'plan_digest': digest(body)}


def infeasible(scored, mode, benefits, reason, violations=None, force=(), forbid=(), maximum_nodes=500000):
    body = {
        'score_digest': scored['score_digest'],
        'mode': mode,
        'status': 'infeasible',
        'optimality_proven': True,
        'selected': [],
        'reason': reason,
        'force': sorted(force),
        'forbid': sorted(forbid),
        'maximum_nodes': maximum_nodes,
        'violations': [] if violations is None else violations,
        'benefits': [benefits[key] for key in sorted(benefits)],
    }
    return {**body, 'plan_digest': digest(body)}


def verify(catalog, scored, plan, replay=False):
    if plan['score_digest'] != scored['score_digest']:
        raise ValueError('plan score binding mismatch')
    if digest({key: value for key, value in plan.items() if key != 'plan_digest'}) != plan['plan_digest']:
        raise ValueError('plan checksum mismatch')
    if plan['status'] == 'infeasible':
        if replay and optimize(catalog, scored, plan['mode'], plan['force'], plan['forbid'], plan['maximum_nodes']) != plan:
            raise ValueError('infeasible plan replay mismatch')
        return {'valid': True, 'feasible': False}
    checked = evaluate(catalog, scored, plan['selected'])
    if not checked['feasible'] or checked != plan['constraint_check']:
        raise ValueError('portfolio constraint certificate mismatch')
    expected = benefits(catalog, scored, plan['mode'])
    calculated = objective(expected, plan['selected'])
    if exact(calculated[0]) != plan['objective']['exposure_reduction']:
        raise ValueError('portfolio benefit does not conserve')
    if -calculated[1] != plan['objective']['cost_cents'] or -calculated[2] != plan['objective']['outage_cost_cents']:
        raise ValueError('portfolio money or outage mismatch')
    if replay:
        reproduced = optimize(catalog, scored, plan['mode'], plan['force'], plan['forbid'], plan['search']['maximum_nodes'])
        if reproduced != plan:
            raise ValueError('portfolio optimizer replay mismatch')
    return {'valid': True, 'feasible': True, 'selected': len(plan['selected']), 'replayed': replay}
