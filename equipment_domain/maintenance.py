from collections import defaultdict
from equipment_domain.values import day, amount, exact, integer
from equipment_domain.evidence import active, lineage


def union(intervals):
    output = []
    for start, end in sorted(intervals):
        if start >= end:
            raise ValueError('empty maintenance interval')
        if output and start <= output[-1][1]:
            output[-1] = (output[-1][0], max(output[-1][1], end))
        else:
            output.append((start, end))
    return output


def interval_days(intervals):
    return sum((end - start).days for start, end in union(intervals))


def history(catalog, start, end, known_at):
    first = day(start, 'start')
    last = day(end, 'end')
    knowledge = day(known_at, 'known_at')
    if not first < last <= knowledge:
        raise ValueError('positive report window ending no later than knowledge required')
    output = []
    for asset_id, asset in sorted(catalog.assets.items()):
        records = [row for row in catalog.maintenance.values() if row['asset'] == asset_id and day(row['recorded'], 'recorded') <= knowledge and day(row['start'], 'start') < last and day(row['end'], 'end') > first]
        intervals = [(max(first, day(row['start'], 'start')), min(last, day(row['end'], 'end'))) for row in records]
        counts = defaultdict(int)
        costs = defaultdict(int)
        for row in records:
            counts[row['kind']] += 1
            costs[row['kind']] += amount(row['cost'], 'cost')
        service_start = max(first, day(asset['commissioned'], 'commissioned'))
        service_end = min(last, day(asset['retired'], 'retired')) if asset['retired'] else last
        exposure = max(0, (service_end - service_start).days)
        in_service = []
        for left, right in intervals:
            left, right = max(left, service_start), min(right, service_end)
            if left < right:
                in_service.append((left, right))
        unavailable = interval_days(in_service)
        output.append({
            'asset': asset_id,
            'site': asset['site'],
            'class': asset['class'],
            'records': sorted(row['id'] for row in records),
            'record_count_by_kind': dict(sorted(counts.items())),
            'whole_record_cost_cents_by_kind': dict(sorted(costs.items())),
            'whole_record_cost_cents': sum(costs.values()),
            'union_outage_days': unavailable,
            'in_service_days': exposure,
            'available_days': exposure - unavailable,
            'availability': {'numerator': exposure - unavailable, 'denominator': exposure} if exposure else None,
            'outage_intervals': [{'start': left.isoformat(), 'end': right.isoformat()} for left, right in union(in_service)],
        })
    return {'start': start, 'end': end, 'known_at': known_at, 'rows': output, 'cost_rule': 'full_record_cost_if_interval_overlaps_window_no_unjustified_proration', 'no_failure_rate_inferred_from_maintenance_count': True}


def overlaps(catalog, known_at):
    knowledge = day(known_at, 'known_at')
    groups = defaultdict(list)
    for row in catalog.maintenance.values():
        if day(row['recorded'], 'recorded') <= knowledge:
            groups[row['asset']].append(row)
    output = []
    for key, records in sorted(groups.items()):
        records.sort(key=lambda row: (row['start'], row['id']))
        for index, left in enumerate(records):
            for right in records[index + 1:]:
                start = max(day(left['start'], 'start'), day(right['start'], 'start'))
                end = min(day(left['end'], 'end'), day(right['end'], 'end'))
                if start < end:
                    output.append({'asset': key, 'records': [left['id'], right['id']], 'overlap_days': (end - start).days})
    return {'rows': output, 'overlap_is_warning_not_automatic_duplicate': True}


def operating_inventory(catalog, as_of):
    counts = defaultdict(lambda: {'active': 0, 'retired': 0, 'not_commissioned': 0})
    point = day(as_of, 'as_of')
    for asset in catalog.assets.values():
        key = (asset['site'], asset['class'])
        if active(asset, as_of):
            counts[key]['active'] += 1
        elif day(asset['commissioned'], 'commissioned') > point:
            counts[key]['not_commissioned'] += 1
        else:
            counts[key]['retired'] += 1
    output = []
    for site in catalog.sites.values():
        for class_id in catalog.classes:
            if (site['id'], class_id) not in counts and class_id not in site['minimum_operating']:
                continue
            current = counts[(site['id'], class_id)]
            floor = site['minimum_operating'].get(class_id, 0)
            output.append({'site': site['id'], 'class': class_id, **current, 'minimum_operating': floor, 'shortfall': max(0, floor - current['active']), 'spare_above_floor': max(0, current['active'] - floor)})
    return {'as_of': as_of, 'rows': sorted(output, key=lambda row: (row['site'], row['class'])), 'inventory_counts_not_assumed_operational_health': True}


def component_warnings(catalog, as_of):
    output = []
    for key, asset in catalog.assets.items():
        ancestors = lineage(catalog, key)['ancestors']
        if not active(asset, as_of):
            continue
        inactive = [parent for parent in ancestors if not active(catalog.assets[parent], as_of)]
        if inactive:
            output.append({'asset': key, 'inactive_ancestors': inactive, 'code': 'active_component_under_inactive_parent'})
    return {'as_of': as_of, 'rows': sorted(output, key=lambda row: row['asset']), 'does_not_silently_retire_components': True}
