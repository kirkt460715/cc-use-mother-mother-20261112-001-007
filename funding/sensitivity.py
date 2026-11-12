import copy
from fractions import Fraction
from equipment_domain.catalog import Catalog
from equipment_domain.values import integer, exact, money, amount, digest
from risk_model.scoring import score, value
from funding.optimizer import optimize


def budget_frontier(catalog, scored, period_id, budgets, maximum_nodes=100000):
    if period_id not in catalog.periods:
        raise KeyError(period_id)
    if not isinstance(budgets, list) or not budgets or len(budgets) > 30:
        raise ValueError('one to thirty budget points required')
    cents = [amount(item, 'budget') for item in budgets]
    if cents != sorted(set(cents)):
        raise ValueError('budget points must strictly increase')
    rows = []
    previous = None
    for budget in budgets:
        document = catalog.export()
        target = next(row for row in document['periods'] if row['id'] == period_id)
        target['budget'] = budget
        variant = Catalog(document)
        risk = score(variant, scored['as_of'], scored['known_at'], scored['policy'])
        result = optimize(variant, risk, maximum_nodes=maximum_nodes)
        feasible = result['status'] != 'infeasible'
        improvement = value(result['objective']['exposure_reduction']) if feasible else None
        if previous is not None and improvement is not None and result['optimality_proven'] and previous['optimality_proven'] and previous['benefit'] is not None and improvement < previous['benefit']:
            raise AssertionError('exact optimum decreased with larger budget')
        rows.append({
            'budget': budget,
            'status': result['status'],
            'selected': result['selected'],
            'exposure_reduction': exact(improvement) if improvement is not None else None,
            'cost_cents': result['objective']['cost_cents'] if feasible else None,
            'optimality_proven': result['optimality_proven'],
            'plan_digest': result['plan_digest'],
        })
        previous = {'benefit': improvement, 'optimality_proven': result['optimality_proven']}
    return {'period': period_id, 'rows': rows, 'independent_budget_scenarios_no_cross_period_transfers': True}


def uncertainty_portfolios(catalog, scored, maximum_nodes=100000):
    plans = {mode: optimize(catalog, scored, mode, maximum_nodes=maximum_nodes) for mode in ('low', 'central', 'high')}
    sets = [set(plan['selected']) for plan in plans.values() if plan['status'] != 'infeasible']
    stable = set.intersection(*sets) if sets else set()
    possible = set.union(*sets) if sets else set()
    return {
        'plans': plans,
        'selected_in_all_feasible_bounds': sorted(stable),
        'selected_in_some_bounds': sorted(possible),
        'selection_sensitive_to_evidence': sorted(possible - stable),
        'scope': 'three_bound_objectives_not_robust_or_stochastic_optimization',
    }


def weight_shift(catalog, scored, source, target, amounts, maximum_nodes=100000):
    from risk_model.policy import variant
    weights = scored['policy']['weights']
    if source not in weights or target not in weights or source == target:
        raise ValueError('two distinct known factors required')
    if not isinstance(amounts, list) or not amounts or len(amounts) > 20:
        raise ValueError('bounded weight shifts required')
    output = []
    seen = set()
    for shift in amounts:
        integer(shift, 'shift', 0, weights[source])
        if shift in seen:
            raise ValueError('duplicate shift')
        seen.add(shift)
        revised = dict(weights)
        revised[source] -= shift
        revised[target] += shift
        policy = variant(scored['policy'], weights=revised)
        risk = score(catalog, scored['as_of'], scored['known_at'], policy)
        plan = optimize(catalog, risk, maximum_nodes=maximum_nodes)
        output.append({'shift': shift, 'weights': revised, 'ranking': risk['ranking'], 'selected': plan['selected'], 'status': plan['status'], 'policy_digest': digest(policy)})
    return {'source_factor': source, 'target_factor': target, 'rows': output}


def crew_frontier(catalog, scored, period_id, capacities):
    if period_id not in catalog.periods:
        raise KeyError(period_id)
    if not isinstance(capacities, list) or len(capacities) > 20:
        raise ValueError('bounded crew capacities required')
    rows = []
    for capacity in capacities:
        integer(capacity, 'capacity', 0, 100000)
        document = catalog.export()
        next(row for row in document['periods'] if row['id'] == period_id)['crew_days'] = capacity
        variant = Catalog(document)
        risk = score(variant, scored['as_of'], scored['known_at'], scored['policy'])
        plan = optimize(variant, risk)
        rows.append({'crew_days': capacity, 'selected': plan['selected'], 'status': plan['status'], 'objective': plan.get('objective')})
    return {'period': period_id, 'rows': rows, 'does_not_schedule_individual_workers': True}
