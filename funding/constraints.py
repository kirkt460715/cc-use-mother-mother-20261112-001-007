from collections import defaultdict
from equipment_domain.values import amount, day, exact, digest, string_set
from equipment_domain.evidence import active
from risk_model.scoring import value


def dependency_order(catalog):
    state = {}
    order = []
    def visit(key):
        if state.get(key) == 1:
            raise ValueError('cycle in proposal requirements')
        if state.get(key) == 2:
            return
        state[key] = 1
        proposal = catalog.proposals[key]
        for prerequisite in sorted(proposal['requires']):
            prior = catalog.proposals[prerequisite]
            if day(catalog.periods[prior['period']]['start'], 'start') > day(catalog.periods[proposal['period']]['start'], 'start'):
                raise ValueError('proposal requires a later procurement period')
            visit(prerequisite)
        state[key] = 2
        order.append(key)
    for key in sorted(catalog.proposals):
        visit(key)
    return order


def closure(catalog, selected):
    pending = list(selected)
    result = set()
    while pending:
        key = pending.pop()
        if key not in catalog.proposals:
            raise KeyError(key)
        if key in result:
            continue
        result.add(key)
        pending.extend(catalog.proposals[key]['requires'])
    return result


def incompatibilities(catalog):
    pairs = set()
    values = list(catalog.proposals.values())
    for row in values:
        for other in row['excludes']:
            pairs.add(tuple(sorted((row['id'], other))))
    for i, left in enumerate(values):
        for right in values[i + 1:]:
            if left['asset'] == right['asset']:
                pairs.add(tuple(sorted((left['id'], right['id']))))
    return sorted(pairs)


def evaluate(catalog, scored, selected, require_mandatory=True):
    selected = string_set(list(selected), 'selected', 22)
    if selected - catalog.proposals.keys():
        raise ValueError('unknown selected proposal')
    by_asset = {row['asset']: row for row in scored['rows']}
    failures = []
    if require_mandatory:
        for key, proposal in catalog.proposals.items():
            if proposal['mandatory'] and key not in selected:
                failures.append({'code': 'mandatory_missing', 'proposal': key})
    for key in sorted(selected):
        proposal = catalog.proposals[key]
        asset = catalog.assets[proposal['asset']]
        if not by_asset[proposal['asset']]['active']:
            failures.append({'code': 'inactive_asset', 'proposal': key})
        period = catalog.periods[proposal['period']]
        if day(period['start'], 'period.start') < day(scored['as_of'], 'as_of'):
            failures.append({'code': 'period_already_started', 'proposal': key})
        if asset['retired'] is not None and day(asset['retired'], 'retired') <= day(period['start'], 'start'):
            failures.append({'code': 'retired_before_work', 'proposal': key})
        missing = set(proposal['requires']) - selected
        if missing:
            failures.append({'code': 'requires_missing', 'proposal': key, 'missing': sorted(missing)})
        if proposal['outage_days'] > (day(period['end'], 'end') - day(period['start'], 'start')).days:
            failures.append({'code': 'outage_exceeds_period', 'proposal': key})
    for left, right in incompatibilities(catalog):
        if {left, right} <= selected:
            failures.append({'code': 'mutually_exclusive', 'proposals': [left, right]})
    periods = []
    exposure = defaultdict(int)
    for key, period in sorted(catalog.periods.items()):
        members = [catalog.proposals[ident] for ident in selected if catalog.proposals[ident]['period'] == key]
        cost = sum(amount(row['cost'], 'cost') for row in members)
        crew = sum(row['crew_days'] for row in members)
        budget = amount(period['budget'], 'budget')
        if cost > budget:
            failures.append({'code': 'budget_exceeded', 'period': key, 'excess_cents': cost - budget})
        if crew > period['crew_days']:
            failures.append({'code': 'crew_exceeded', 'period': key, 'excess_days': crew - period['crew_days']})
        unavailable = defaultdict(set)
        for row in members:
            if row['outage_days']:
                asset = catalog.assets[row['asset']]
                unavailable[(asset['site'], asset['class'])].add(asset['id'])
        for (site_id, class_id), offline in sorted(unavailable.items()):
            site = catalog.sites[site_id]
            operating = [asset for asset in catalog.assets.values() if asset['site'] == site_id and asset['class'] == class_id and active(asset, period['start'])]
            floor = site['minimum_operating'].get(class_id, 0)
            remaining = len(operating) - len(offline)
            if remaining < floor:
                failures.append({'code': 'site_operating_floor', 'site': site_id, 'class': class_id, 'period': key, 'remaining': remaining, 'required': floor})
            exposure[(site_id, key)] += sum(row['outage_days'] for row in members if catalog.assets[row['asset']]['site'] == site_id and catalog.assets[row['asset']]['class'] == class_id)
        periods.append({'period': key, 'selected': sorted(row['id'] for row in members), 'cost_cents': cost, 'budget_cents': budget, 'unspent_cents': budget - cost, 'crew_days': crew, 'crew_capacity': period['crew_days']})
    return {
        'feasible': not failures,
        'selected': sorted(selected),
        'violations': failures,
        'periods': periods,
        'total_cost_cents': sum(row['cost_cents'] for row in periods),
        'outage_rule': 'conservative_simultaneous_outage_within_each_period_no_intra_period_scheduling',
        'site_outage_asset_days': [{'site': site, 'period': period, 'asset_days': days} for (site, period), days in sorted(exposure.items())],
    }
