from collections import defaultdict
from equipment_domain.values import day, exact, digest


def active(asset, as_of):
    point = day(as_of, 'as_of')
    return day(asset['commissioned'], 'commissioned') <= point and (asset['retired'] is None or point < day(asset['retired'], 'retired'))


def visible(catalog, as_of, known_at):
    business = day(as_of, 'as_of')
    known = day(known_at, 'known_at')
    if known < business:
        raise ValueError('knowledge day must not precede business day')
    inspections = defaultdict(list)
    maintenance = defaultdict(list)
    excluded = []
    for row in catalog.inspections.values():
        if day(row['recorded'], 'recorded') > known:
            excluded.append({'kind': 'inspection', 'id': row['id'], 'reason': 'not_recorded_yet'})
        elif day(row['performed'], 'performed') > business:
            excluded.append({'kind': 'inspection', 'id': row['id'], 'reason': 'future_performance'})
        else:
            inspections[row['asset']].append(row)
    for row in catalog.maintenance.values():
        if day(row['recorded'], 'recorded') > known:
            excluded.append({'kind': 'maintenance', 'id': row['id'], 'reason': 'not_recorded_yet'})
        elif day(row['end'], 'end') > business:
            excluded.append({'kind': 'maintenance', 'id': row['id'], 'reason': 'not_completed'})
        else:
            maintenance[row['asset']].append(row)
    return inspections, maintenance, excluded


def resolve(catalog, as_of, known_at):
    inspections, maintenance, excluded = visible(catalog, as_of, known_at)
    point = day(as_of, 'as_of')
    output = []
    for key, asset in sorted(catalog.assets.items()):
        definition = catalog.classes[asset['class']]
        states = []
        for check in sorted(definition['checks']):
            candidates = [row for row in inspections[key] if check in row['checks'] and row['checks'][check] is not None]
            latest_day = max((row['performed'] for row in candidates), default=None)
            latest = [row for row in candidates if row['performed'] == latest_day]
            severities = {row['checks'][check] for row in latest}
            conflicts = len(severities) > 1
            value = max(severities, default=None)
            age = (point - day(latest_day, 'performed')).days if latest_day else None
            expired = age is not None and age > definition['inspection_valid_days']
            repairs = [row for row in maintenance[key] if check in row['restores_checks'] and (latest_day is None or row['end'] > latest_day)]
            repairs.sort(key=lambda row: (row['end'], row['id']))
            repair = repairs[-1] if repairs else None
            if repair is not None:
                status = 'requires_reinspection_after_repair'
                effective_value = None
            elif conflicts:
                status = 'conflicting_same_day_evidence'
                effective_value = value
            elif latest_day is None:
                status = 'missing'
                effective_value = None
            elif expired:
                status = 'expired'
                effective_value = None
            else:
                status = 'current'
                effective_value = value
            states.append({
                'check': check,
                'status': status,
                'severity': effective_value,
                'last_observed_severity': value,
                'inspection_age_days': age,
                'evidence_ids': sorted(row['id'] for row in latest),
                'repair_evidence': repair['id'] if repair else None,
                'conflicting': conflicts,
                'expired': expired,
            })
        current = sum(row['status'] == 'current' for row in states)
        output.append({
            'asset': key,
            'site': asset['site'],
            'class': asset['class'],
            'active': active(asset, as_of),
            'age_days': max(0, (point - day(asset['commissioned'], 'commissioned')).days),
            'checks': states,
            'current_checks': current,
            'required_checks': len(states),
            'coverage': {'numerator': current, 'denominator': len(states)},
            'maintenance_ids': sorted(row['id'] for row in maintenance[key]),
            'evidence_digest': digest([
                {field: value for field, value in state.items()
                 if field != 'inspection_age_days'}
                for state in states
            ]),
        })
    return {'as_of': as_of, 'known_at': known_at, 'rows': output, 'excluded': sorted(excluded, key=lambda row: (row['kind'], row['id'])), 'repair_does_not_reset_inspection_severity_without_reinspection': True}


def lineage(catalog, asset_id):
    if asset_id not in catalog.assets:
        raise KeyError(asset_id)
    ancestors = []
    current = catalog.assets[asset_id]['parent']
    while current is not None:
        ancestors.append(current)
        current = catalog.assets[current]['parent']
    children = defaultdict(list)
    for asset in catalog.assets.values():
        if asset['parent'] is not None:
            children[asset['parent']].append(asset['id'])
    pending = list(sorted(children[asset_id], reverse=True))
    descendants = []
    while pending:
        current = pending.pop()
        descendants.append(current)
        pending.extend(sorted(children[current], reverse=True))
    return {'asset': asset_id, 'ancestors': ancestors, 'descendants': descendants, 'direct_children': sorted(children[asset_id])}


def evidence_delta(before, after):
    if before['as_of'] != after['as_of']:
        raise ValueError('evidence comparison requires fixed business day')
    if day(before['known_at'], 'known_at') >= day(after['known_at'], 'known_at'):
        raise ValueError('knowledge must move forward')
    first = {row['asset']: row for row in before['rows']}
    last = {row['asset']: row for row in after['rows']}
    changes = []
    for key in sorted(first.keys() | last.keys()):
        left, right = first.get(key), last.get(key)
        if left == right:
            continue
        changes.append({'asset': key, 'before_digest': left['evidence_digest'] if left else None, 'after_digest': right['evidence_digest'] if right else None, 'before_current': left['current_checks'] if left else None, 'after_current': right['current_checks'] if right else None})
    return {'as_of': before['as_of'], 'before_knowledge': before['known_at'], 'after_knowledge': after['known_at'], 'changes': changes}
