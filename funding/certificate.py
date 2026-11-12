from fractions import Fraction
from equipment_domain.values import amount, exact
from funding.optimizer import benefits
from risk_model.scoring import value


def relaxed_bound(catalog, scored, mode='central', resource='money'):
    if resource not in ('money', 'crew'):
        raise ValueError('bound resource must be money or crew')
    benefit = benefits(catalog, scored, mode)
    rows = []
    for period_id, period in sorted(catalog.periods.items()):
        capacity = amount(period['budget'], 'budget') if resource == 'money' else period['crew_days']
        candidates = []
        free = Fraction(0)
        free_ids = []
        for key, proposal in sorted(catalog.proposals.items()):
            if proposal['period'] != period_id:
                continue
            gain = value(benefit[key]['exposure_reduction'])
            cost = amount(proposal['cost'], 'cost') if resource == 'money' else proposal['crew_days']
            if cost == 0:
                free += gain
                free_ids.append(key)
            else:
                candidates.append((gain / cost, key, cost, gain))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        remaining = Fraction(capacity)
        upper = free
        fractions = []
        for density, key, cost, gain in candidates:
            if remaining <= 0:
                break
            used = min(Fraction(1), remaining / cost)
            upper += gain * used
            remaining -= cost * used
            fractions.append({'proposal': key, 'fraction': exact(used), 'benefit': exact(gain * used)})
        rows.append({
            'period': period_id,
            'capacity': capacity,
            'unused_relaxed_resource': exact(remaining),
            'zero_resource_proposals': free_ids,
            'fractional_choices': fractions,
            'upper_bound': exact(upper),
        })
    total = sum((value(row['upper_bound']) for row in rows), Fraction(0))
    return {
        'resource': resource,
        'rows': rows,
        'upper_bound': exact(total),
        'relaxations': ['fractional_candidates', 'ignore_exclusions_and_same_asset', 'ignore_dependencies', 'ignore_other_resource', 'ignore_operating_floor_and_retirement'],
    }


def gap(catalog, scored, plan):
    if plan['status'] == 'infeasible':
        return {'available': False, 'reason': 'infeasible_plan'}
    money_bound = relaxed_bound(catalog, scored, plan['mode'], 'money')
    crew_bound = relaxed_bound(catalog, scored, plan['mode'], 'crew')
    upper = min(value(money_bound['upper_bound']), value(crew_bound['upper_bound']))
    attained = value(plan['objective']['exposure_reduction'])
    if upper < attained:
        raise ValueError('relaxed bound below feasible attained objective')
    difference = upper - attained
    return {
        'available': True,
        'attained': exact(attained),
        'upper_bound': exact(upper),
        'absolute_gap_at_most': exact(difference),
        'relative_gap_at_most': exact(difference / upper) if upper else exact(0),
        'search_proved_optimal': plan['optimality_proven'],
        'money_relaxation': money_bound,
        'crew_relaxation': crew_bound,
        'relaxed_bound_may_remain_loose_even_when_exact_search_is_complete': True,
    }


def resource_slack(catalog, scored, plan):
    if plan['status'] == 'infeasible':
        raise ValueError('slack requires feasible plan')
    output = []
    benefit = {row['proposal']: row for row in plan['benefits']}
    for row in plan['constraint_check']['periods']:
        chosen = row['selected']
        total = sum((value(benefit[key]['exposure_reduction']) for key in chosen), Fraction(0))
        output.append({
            'period': row['period'],
            'money_used_cents': row['cost_cents'],
            'money_unused_cents': row['unspent_cents'],
            'crew_used_days': row['crew_days'],
            'crew_unused_days': row['crew_capacity'] - row['crew_days'],
            'money_utilization': exact(Fraction(row['cost_cents'], row['budget_cents'])) if row['budget_cents'] else None,
            'crew_utilization': exact(Fraction(row['crew_days'], row['crew_capacity'])) if row['crew_capacity'] else None,
            'exposure_reduction': exact(total),
            'binding_resources': [key for key, used, limit in [('money', row['cost_cents'], row['budget_cents']), ('crew', row['crew_days'], row['crew_capacity'])] if used == limit],
        })
    return {'rows': output, 'binding_does_not_imply_positive_shadow_price_for_discrete_problem': True}
