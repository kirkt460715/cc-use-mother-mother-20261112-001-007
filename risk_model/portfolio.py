from collections import defaultdict
from fractions import Fraction
from equipment_domain.values import exact, integer
from risk_model.scoring import value


def summary(scored, dimension='region'):
    if dimension not in ('region', 'site', 'class', 'rural', 'band'):
        raise ValueError('unsupported portfolio dimension')
    groups = defaultdict(list)
    for row in scored['rows']:
        if row['active']:
            groups[row[dimension]].append(row)
    output = []
    for key, rows in sorted(groups.items()):
        central = sum((value(row['central']) for row in rows), Fraction(0))
        low = sum((value(row['low']) for row in rows), Fraction(0))
        high = sum((value(row['high']) for row in rows), Fraction(0))
        bands = {name: sum(row['band'] == name for row in rows) for name in ('routine', 'watch', 'urgent', 'critical')}
        output.append({
            dimension: key,
            'assets': len(rows),
            'risk_total': exact(central),
            'mean_score': exact(central / len(rows)),
            'mean_low': exact(low / len(rows)),
            'mean_high': exact(high / len(rows)),
            'maximum_score': exact(max(value(row['central']) for row in rows)),
            'evidence_review_assets': sum(row['requires_evidence_review'] for row in rows),
            'bands': bands,
            'count_unit': 'asset_not_site_or_unique_service_user',
        })
    return {'dimension': dimension, 'rows': output, 'score_digest': scored['score_digest']}


def investment_distribution(catalog, scored, plan, dimension='region'):
    if dimension not in ('region', 'site', 'class', 'rural'):
        raise ValueError('unsupported investment dimension')
    if plan['status'] == 'infeasible':
        raise ValueError('investment report requires feasible plan')
    assets = {row['asset']: row for row in scored['rows'] if row['active']}
    benefit = {row['proposal']: row for row in plan['benefits']}
    groups = defaultdict(lambda: {'assets': [], 'selected': [], 'cost_cents': 0, 'benefit': Fraction(0)})
    for asset_id, row in assets.items():
        groups[row[dimension]]['assets'].append(asset_id)
    for key in plan['selected']:
        proposal = catalog.proposals[key]
        row = assets[proposal['asset']]
        group = groups[row[dimension]]
        group['selected'].append(key)
        group['cost_cents'] += benefit[key]['cost_cents']
        group['benefit'] += value(benefit[key]['exposure_reduction'])
    output = []
    total = plan['objective']['cost_cents']
    for key, group in sorted(groups.items()):
        output.append({
            dimension: key,
            'active_assets': len(group['assets']),
            'selected_proposals': len(group['selected']),
            'cost_cents': group['cost_cents'],
            'budget_share': exact(Fraction(group['cost_cents'], total)) if total else None,
            'declared_exposure_reduction': exact(group['benefit']),
            'selected_ids': sorted(group['selected']),
        })
    if sum(row['cost_cents'] for row in output) != total:
        raise ValueError('investment aggregation does not conserve money')
    return {'dimension': dimension, 'rows': output, 'does_not_claim_equitable_allocation_from_equal_spending': True}


def public_summary(scored, dimension='region', minimum_sites=3):
    integer(minimum_sites, 'minimum_sites', 1, 100000)
    if dimension not in ('region', 'class', 'rural'):
        raise ValueError('public grouping must not expose site or asset identity')
    report = summary(scored, dimension)
    sites = defaultdict(set)
    for row in scored['rows']:
        if row['active']:
            sites[row[dimension]].add(row['site'])
    suppressed = {key for key, values in sites.items() if len(values) < minimum_sites}
    if len(suppressed) == 1 and len(sites) > 1:
        eligible = set(sites) - suppressed
        suppressed.add(min(eligible, key=lambda key: (len(sites[key]), str(key))))
    output = []
    for row in report['rows']:
        key = row[dimension]
        if key in suppressed:
            output.append({dimension: key, 'suppressed': True})
        else:
            output.append({**row, 'sites': len(sites[key]), 'suppressed': False})
    return {'dimension': dimension, 'minimum_sites': minimum_sites, 'rows': output, 'contains_asset_or_site_ids': False, 'total_omitted': bool(suppressed), 'privacy_scope': 'partition_and_complement_suppression_not_composition_privacy'}


def calibration_check(scored, outcomes):
    if not isinstance(outcomes, list) or len(outcomes) > 10000:
        raise ValueError('bounded outcome records required')
    by_asset = {row['asset']: row for row in scored['rows'] if row['active']}
    seen = set()
    groups = defaultdict(list)
    for row in outcomes:
        if not isinstance(row, dict) or set(row) != {'asset', 'failed'} or type(row['failed']) is not bool:
            raise ValueError('outcome requires asset and boolean failed')
        if row['asset'] not in by_asset or row['asset'] in seen:
            raise ValueError('unknown or duplicate outcome asset')
        seen.add(row['asset'])
        groups[by_asset[row['asset']]['band']].append(row['failed'])
    output = []
    for band in ('routine', 'watch', 'urgent', 'critical'):
        values = groups[band]
        output.append({'band': band, 'observed_assets': len(values), 'failures': sum(values), 'failure_fraction': exact(Fraction(sum(values), len(values))) if values else None})
    return {'rows': output, 'unobserved_assets': sorted(set(by_asset) - seen), 'risk_score_is_not_probability_no_brier_score_reported': True, 'no_automatic_weight_fitting': True}
