from fractions import Fraction
from equipment_domain.values import exact, ratio, digest
from equipment_domain.evidence import resolve
from risk_model.policy import validate, contribution, factor_bounds, band


def value(encoded):
    return Fraction(encoded['numerator'], encoded['denominator'])


def score(catalog, as_of, known_at, policy=None):
    policy = catalog.policy if policy is None else policy
    validate(policy)
    evidence = resolve(catalog, as_of, known_at)
    rows = []
    for observation in evidence['rows']:
        asset = catalog.assets[observation['asset']]
        definition = catalog.classes[asset['class']]
        factors = factor_bounds(asset, definition, observation, policy)
        calculated = {key: contribution(factors[key], policy) for key in ('low', 'central', 'high')}
        central = calculated['central']
        coverage = Fraction(observation['current_checks'] * 100, observation['required_checks'])
        trustworthy = coverage >= policy['minimum_evidence_pct'] and not factors['unresolved_checks']
        site = catalog.sites[asset['site']]
        rows.append({
            'asset': asset['id'],
            'site': asset['site'],
            'region': site['region'],
            'rural': site['rural'],
            'class': asset['class'],
            'active': observation['active'],
            'central': central['score'],
            'low': calculated['low']['score'],
            'high': calculated['high']['score'],
            'band': central['band'],
            'possible_bands': sorted({band(value(calculated[key]['score']), policy) for key in calculated}),
            'factors': central['factors'],
            'coverage_pct': exact(coverage),
            'requires_evidence_review': not trustworthy,
            'unresolved_checks': factors['unresolved_checks'],
            'users_exposed': site['users'],
            'evidence_digest': observation['evidence_digest'],
        })
    active_rows = [row for row in rows if row['active']]
    ranked = sorted(active_rows, key=lambda row: (-value(row['central']), -value(row['high']), row['asset']))
    for rank, row in enumerate(ranked, 1):
        row['priority_rank'] = rank
    body = {
        'input_digest': catalog.fingerprint,
        'as_of': as_of,
        'known_at': known_at,
        'policy': policy,
        'policy_digest': digest(policy),
        'evidence': evidence,
        'rows': sorted(rows, key=lambda row: row['asset']),
        'ranking': [row['asset'] for row in ranked],
        'risk_unit': 'transparent_ordinal_index_not_failure_probability',
        'missing_evidence_is_not_a_clean_inspection': True,
    }
    return {**body, 'score_digest': digest(body)}


def verify(catalog, scored, replay=True):
    if scored['input_digest'] != catalog.fingerprint:
        raise ValueError('score source binding mismatch')
    if digest({key: value for key, value in scored.items() if key != 'score_digest'}) != scored['score_digest']:
        raise ValueError('score digest mismatch')
    if digest(scored['policy']) != scored['policy_digest']:
        raise ValueError('score policy binding mismatch')
    for row in scored['rows']:
        if not 0 <= value(row['low']) <= value(row['central']) <= value(row['high']) <= 100:
            raise ValueError('risk interval does not enclose central score')
        if sum(value(item['contribution']) for item in row['factors']) != value(row['central']):
            raise ValueError('factor contributions do not conserve score')
    if replay and score(catalog, scored['as_of'], scored['known_at'], scored['policy']) != scored:
        raise ValueError('risk scoring replay mismatch')
    return {'valid': True, 'assets': len(scored['rows']), 'active': len(scored['ranking']), 'replayed': replay}


def priority_stability(scored):
    active = [row for row in scored['rows'] if row['active']]
    rows = []
    for row in active:
        definitely_above = [other['asset'] for other in active if value(other['low']) > value(row['high'])]
        potentially_above = [other['asset'] for other in active if other['asset'] != row['asset'] and value(other['high']) >= value(row['low'])]
        rows.append({
            'asset': row['asset'],
            'central_rank': row['priority_rank'],
            'best_possible_rank': len(definitely_above) + 1,
            'worst_possible_rank': len(potentially_above) + 1,
            'definitely_higher': sorted(definitely_above),
            'ambiguous_evidence': row['requires_evidence_review'],
        })
    return {'rows': sorted(rows, key=lambda row: row['central_rank']), 'bounds_are_pairwise_conservative_not_joint_rank_distribution': True}


def inspection_value(scored):
    rows = []
    for row in scored['rows']:
        if not row['active']:
            continue
        width = value(row['high']) - value(row['low'])
        if width == 0 and not row['unresolved_checks']:
            continue
        rows.append({'asset': row['asset'], 'score_interval_width': exact(width), 'checks_to_resolve': row['unresolved_checks'], 'priority': exact(width * max(1, row['users_exposed'])), 'scope': 'uncertainty_exposure_index_not_expected_value_of_information'})
    rows.sort(key=lambda row: (-value(row['priority']), row['asset']))
    return {'rows': rows, 'does_not_assume_inspection_always_lowers_risk': True}
