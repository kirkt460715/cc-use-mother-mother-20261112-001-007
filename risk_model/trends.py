from collections import defaultdict
from fractions import Fraction
from equipment_domain.values import day, exact, digest, rows
from risk_model.scoring import score, value


def series(catalog, dates, known_at):
    rows(dates, 'dates', 36)
    if not dates or dates != sorted(set(dates)):
        raise ValueError('strictly increasing business dates required')
    knowledge = day(known_at, 'known_at')
    for date in dates:
        if day(date, 'date') > knowledge:
            raise ValueError('series date exceeds fixed knowledge cutoff')
    snapshots = [score(catalog, date, known_at) for date in dates]
    output = []
    for asset_id in sorted(catalog.assets):
        observations = []
        previous = None
        for snapshot in snapshots:
            row = next(row for row in snapshot['rows'] if row['asset'] == asset_id)
            current = value(row['central']) if row['active'] else None
            change = current - previous if current is not None and previous is not None else None
            observations.append({
                'date': snapshot['as_of'],
                'active': row['active'],
                'central': row['central'] if row['active'] else None,
                'low': row['low'] if row['active'] else None,
                'high': row['high'] if row['active'] else None,
                'band': row['band'] if row['active'] else None,
                'change_from_previous_active_date': exact(change) if change is not None else None,
                'evidence_digest': row['evidence_digest'],
                'review_required': row['requires_evidence_review'],
            })
            previous = current
        output.append({'asset': asset_id, 'observations': observations})
    return {'known_at': known_at, 'policy_digest': digest(catalog.policy), 'rows': output, 'scope': 'retrospective_fixed_knowledge_not_historical_forecast_backtest'}


def decompose(before, after):
    if before['policy_digest'] != after['policy_digest']:
        raise ValueError('factor change attribution requires same scoring policy')
    first = {row['asset']: row for row in before['rows']}
    last = {row['asset']: row for row in after['rows']}
    output = []
    for key in sorted(first.keys() & last.keys()):
        left, right = first[key], last[key]
        if not left['active'] or not right['active']:
            continue
        old = {row['factor']: row for row in left['factors']}
        new = {row['factor']: row for row in right['factors']}
        factors = []
        for factor in sorted(old):
            difference = value(new[factor]['contribution']) - value(old[factor]['contribution'])
            factors.append({'factor': factor, 'contribution_change': exact(difference), 'before_raw': old[factor]['raw'], 'after_raw': new[factor]['raw']})
        total = value(right['central']) - value(left['central'])
        if sum(value(row['contribution_change']) for row in factors) != total:
            raise ValueError('factor change attribution does not conserve')
        output.append({'asset': key, 'central_change': exact(total), 'factors': factors, 'evidence_changed': left['evidence_digest'] != right['evidence_digest']})
    return {'rows': output, 'same_policy': True, 'scope': 'algebraic_score_decomposition_not_causal_effect'}


def transitions(before, after, dimension='region'):
    if dimension not in ('region', 'class', 'rural'):
        raise ValueError('unsupported transition grouping')
    first = {row['asset']: row for row in before['rows']}
    last = {row['asset']: row for row in after['rows']}
    groups = defaultdict(int)
    added = []
    removed = []
    for key in sorted(first.keys() | last.keys()):
        left, right = first.get(key), last.get(key)
        was = left is not None and left['active']
        now = right is not None and right['active']
        if not was and now:
            added.append(key)
        elif was and not now:
            removed.append(key)
        elif was and now:
            groups[(right[dimension], left['band'], right['band'])] += 1
    return {
        'dimension': dimension,
        'rows': [{dimension: key, 'from': old, 'to': new, 'assets': count} for (key, old, new), count in sorted(groups.items())],
        'newly_active': added,
        'no_longer_active': removed,
        'scope': 'matched_asset_transitions_grouped_by_destination_attributes',
    }


def evidence_aging(catalog, as_of, known_at, horizons):
    from equipment_domain.values import after, integer
    rows(horizons, 'horizons', 20)
    knowledge = day(known_at, 'known_at')
    output = []
    for horizon in horizons:
        integer(horizon, 'horizon', 0, 366)
        date = after(as_of, horizon)
        if day(date, 'date') > knowledge:
            raise ValueError('aging dates must fit supplied fixed knowledge cutoff')
        risk = score(catalog, date, known_at)
        active = [row for row in risk['rows'] if row['active']]
        output.append({
            'horizon_days': horizon,
            'date': date,
            'active_assets': len(active),
            'requiring_review': sum(row['requires_evidence_review'] for row in active),
            'mean_interval_width': exact(sum((value(row['high']) - value(row['low']) for row in active), Fraction(0)) / len(active)) if active else None,
            'ranking': risk['ranking'],
        })
    return {'rows': output, 'scope': 'recorded_evidence_only_no_forecast_inspection_assumed'}
