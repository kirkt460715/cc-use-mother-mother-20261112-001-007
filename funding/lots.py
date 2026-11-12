from collections import defaultdict
from datetime import timedelta
from equipment_domain.values import day, amount, digest
from equipment_domain.evidence import active
from funding.constraints import dependency_order


def schedule(catalog, scored, plan):
    if plan['status'] == 'infeasible':
        raise ValueError('cannot schedule infeasible portfolio')
    chosen = set(plan['selected'])
    assigned = {}
    blocked = []
    for key in dependency_order(catalog):
        if key not in chosen:
            continue
        proposal = catalog.proposals[key]
        period = catalog.periods[proposal['period']]
        earliest = day(period['start'], 'period.start')
        latest = day(period['end'], 'period.end')
        missing = [prior for prior in proposal['requires'] if prior not in assigned]
        if missing:
            blocked.append({'proposal': key, 'reason': 'prerequisite_not_scheduled', 'prerequisites': missing})
            continue
        for prior in proposal['requires']:
            earliest = max(earliest, day(assigned[prior]['end'], 'prerequisite.end'))
        duration = proposal['outage_days']
        end = earliest + timedelta(days=duration)
        if end > latest:
            blocked.append({'proposal': key, 'reason': 'dependency_chain_exceeds_period', 'earliest_start': earliest.isoformat(), 'would_end': end.isoformat(), 'period_end': latest.isoformat()})
            continue
        assigned[key] = {
            'proposal': key,
            'asset': proposal['asset'],
            'period': proposal['period'],
            'start': earliest.isoformat(),
            'end': end.isoformat(),
            'outage_days': duration,
            'crew_days_reserved_in_period': proposal['crew_days'],
            'cost_cents': amount(proposal['cost'], 'cost'),
        }
    body = {
        'plan_digest': plan['plan_digest'],
        'rows': [assigned[key] for key in sorted(assigned)],
        'blocked': blocked,
        'all_selected_scheduled': len(assigned) == len(chosen),
        'algorithm': 'stable_earliest_start_after_prerequisites',
        'scope': 'candidate_calendar_no_individual_worker_assignment_or_procurement_execution',
    }
    return {**body, 'schedule_digest': digest(body)}


def daily_inventory(catalog, schedule_result):
    changes = defaultdict(lambda: {'begin': [], 'end': []})
    for row in schedule_result['rows']:
        if row['outage_days']:
            changes[row['start']]['begin'].append(row['asset'])
            changes[row['end']]['end'].append(row['asset'])
    if changes:
        first, last = min(changes), max(changes)
        for asset in catalog.assets.values():
            for date in (asset['commissioned'], asset['retired']):
                if date is not None and first < date < last:
                    changes[date]
    boundaries = sorted(changes)
    offline = set()
    rows = []
    for position, date in enumerate(boundaries):
        offline.difference_update(changes[date]['end'])
        offline.update(changes[date]['begin'])
        if position + 1 == len(boundaries):
            continue
        end = boundaries[position + 1]
        for site_id, site in sorted(catalog.sites.items()):
            for class_id, minimum in sorted(site['minimum_operating'].items()):
                inventory = {asset['id'] for asset in catalog.assets.values() if asset['site'] == site_id and asset['class'] == class_id and active(asset, date)}
                unavailable = inventory & offline
                remaining = len(inventory - unavailable)
                rows.append({
                    'start': date,
                    'end': end,
                    'site': site_id,
                    'class': class_id,
                    'active_assets': len(inventory),
                    'offline_assets': sorted(unavailable),
                    'operating': remaining,
                    'minimum': minimum,
                    'shortfall': max(0, minimum - remaining),
                })
    return {'rows': rows, 'no_shortfalls_at_event_boundaries': not any(row['shortfall'] for row in rows), 'scope': 'scheduled_outages_only_existing_maintenance_not_subtracted'}


def audit(catalog, scored, plan, schedule_result):
    if schedule_result['plan_digest'] != plan['plan_digest']:
        raise ValueError('schedule plan binding mismatch')
    if digest({key: value for key, value in schedule_result.items() if key != 'schedule_digest'}) != schedule_result['schedule_digest']:
        raise ValueError('schedule checksum mismatch')
    if schedule(catalog, scored, plan) != schedule_result:
        raise ValueError('schedule replay mismatch')
    assigned = {row['proposal']: row for row in schedule_result['rows']}
    for key, row in assigned.items():
        proposal = catalog.proposals[key]
        for prior in proposal['requires']:
            if prior not in assigned or assigned[prior]['end'] > row['start']:
                raise ValueError('schedule violates dependency completion')
        if (day(row['end'], 'end') - day(row['start'], 'start')).days != proposal['outage_days']:
            raise ValueError('scheduled duration mismatch')
    return {'valid': True, 'scheduled': len(assigned), 'blocked': len(schedule_result['blocked'])}


def lot_summary(catalog, plan, schedule_result):
    scheduled = {row['proposal']: row for row in schedule_result['rows']}
    groups = defaultdict(list)
    for key in plan['selected']:
        proposal = catalog.proposals[key]
        asset = catalog.assets[proposal['asset']]
        groups[(proposal['period'], asset['site'], proposal['action'])].append(key)
    rows = []
    for (period, site, action), members in sorted(groups.items()):
        cost = sum(amount(catalog.proposals[key]['cost'], 'cost') for key in members)
        rows.append({
            'period': period,
            'site': site,
            'action': action,
            'proposals': sorted(members),
            'cost_cents': cost,
            'crew_days': sum(catalog.proposals[key]['crew_days'] for key in members),
            'all_scheduled': all(key in scheduled for key in members),
            'earliest_start': min((scheduled[key]['start'] for key in members if key in scheduled), default=None),
            'latest_end': max((scheduled[key]['end'] for key in members if key in scheduled), default=None),
        })
    return {'rows': rows, 'scope': 'analytical_lot_grouping_not_purchase_order', 'total_cents': sum(row['cost_cents'] for row in rows)}
