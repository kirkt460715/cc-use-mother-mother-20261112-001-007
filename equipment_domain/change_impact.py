from collections import defaultdict
from equipment_domain.values import digest
from equipment_domain.evidence import lineage


TABLES = ('sites', 'classes', 'assets', 'inspections', 'maintenance', 'proposals', 'periods')


def compare(before, after):
    changes = []
    affected_assets = set()
    affected_proposals = set()
    affected_periods = set()
    for table in TABLES:
        first = getattr(before, table)
        last = getattr(after, table)
        for key in sorted(first.keys() | last.keys()):
            left, right = first.get(key), last.get(key)
            if left == right:
                continue
            if left is None:
                action = 'added'
            elif right is None:
                action = 'removed'
            else:
                action = 'changed'
            changed_fields = sorted(field for field in set(left or {}) | set(right or {}) if (left or {}).get(field) != (right or {}).get(field))
            changes.append({
                'table': table,
                'id': key,
                'action': action,
                'fields': changed_fields,
                'before_digest': digest(left) if left else None,
                'after_digest': digest(right) if right else None,
            })
            for catalog, row in ((before, left), (after, right)):
                if row is None:
                    continue
                if table == 'assets':
                    affected_assets.add(key)
                    affected_assets.update(lineage(catalog, key)['descendants'])
                elif table in ('inspections', 'maintenance'):
                    affected_assets.add(row['asset'])
                elif table == 'classes':
                    affected_assets.update(asset['id'] for asset in catalog.assets.values() if asset['class'] == key)
                elif table == 'sites':
                    affected_assets.update(asset['id'] for asset in catalog.assets.values() if asset['site'] == key)
                elif table == 'proposals':
                    affected_proposals.add(key)
                    affected_assets.add(row['asset'])
                    affected_periods.add(row['period'])
                elif table == 'periods':
                    affected_periods.add(key)
    policy_changed = before.policy != after.policy
    if policy_changed:
        affected_assets.update(before.assets.keys() | after.assets.keys())
    for catalog in (before, after):
        for key, proposal in catalog.proposals.items():
            if proposal['asset'] in affected_assets or proposal['period'] in affected_periods:
                affected_proposals.add(key)
                affected_periods.add(proposal['period'])
    dependent = True
    while dependent:
        dependent = False
        for catalog in (before, after):
            for key, proposal in catalog.proposals.items():
                if key in affected_proposals:
                    continue
                if set(proposal['requires']) & affected_proposals or set(proposal['excludes']) & affected_proposals:
                    affected_proposals.add(key)
                    affected_periods.add(proposal['period'])
                    dependent = True
    return {
        'before_digest': before.fingerprint,
        'after_digest': after.fingerprint,
        'changes': changes,
        'policy_changed': policy_changed,
        'potentially_affected_assets': sorted(affected_assets),
        'potentially_affected_proposals': sorted(affected_proposals),
        'potentially_affected_periods': sorted(affected_periods),
        'optimization_must_recompute_all_candidates': bool(changes or policy_changed),
        'scope': 'conservative_dependency_impact_not_claim_that_unchanged_candidate_selection_is_stable',
    }


def patch_plan(before, after):
    difference = compare(before, after)
    operations = []
    for change in difference['changes']:
        table, key = change['table'], change['id']
        row = getattr(after, table).get(key)
        operations.append({'table': table, 'id': key, 'before_digest': change['before_digest'], 'after': row})
    return {
        'operations': operations,
        'policy_replacement': after.policy if difference['policy_changed'] else None,
        'requires_full_catalog_validation_after_atomic_application': True,
        'before_digest': before.fingerprint,
        'after_digest': after.fingerprint,
    }


def site_impact(before, after):
    difference = compare(before, after)
    sites = defaultdict(lambda: {'assets': set(), 'proposals': set()})
    for key in difference['potentially_affected_assets']:
        for catalog in (before, after):
            if key in catalog.assets:
                sites[catalog.assets[key]['site']]['assets'].add(key)
    for key in difference['potentially_affected_proposals']:
        for catalog in (before, after):
            if key in catalog.proposals:
                asset = catalog.assets[catalog.proposals[key]['asset']]
                sites[asset['site']]['proposals'].add(key)
    rows = []
    for site, items in sorted(sites.items()):
        rows.append({'site': site, 'affected_assets': sorted(items['assets']), 'affected_proposals': sorted(items['proposals'])})
    return {'rows': rows, 'policy_changed': difference['policy_changed']}
