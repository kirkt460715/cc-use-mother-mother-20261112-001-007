import base64
import json
from equipment_domain.values import canonical, digest, fields, integer
from risk_model.scoring import priority_stability, inspection_value
from risk_model.portfolio import summary, public_summary, investment_distribution, calibration_check
from funding.explanations import forced_tradeoffs, objective_attribution, dominance
from funding.sensitivity import budget_frontier, uncertainty_portfolios, weight_shift, crew_frontier
from equipment_domain.maintenance import history, overlaps, operating_inventory, component_warnings
from equipment_domain.evidence import lineage


FILTERS = {'site', 'region', 'class', 'active', 'band', 'rural', 'requires_evidence_review'}
COLUMNS = FILTERS | {'asset', 'central', 'low', 'high', 'coverage_pct', 'unresolved_checks', 'users_exposed', 'evidence_digest'}


def encode(value):
    return base64.urlsafe_b64encode(canonical(value).encode()).decode().rstrip('=')


def decode(value):
    if not isinstance(value, str) or len(value) > 10000:
        raise ValueError('invalid query cursor')
    try:
        result = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('malformed query cursor') from exc
    fields(result, ('context', 'offset'))
    return result


def assets(result, filters=None, columns=None, limit=100, cursor=None):
    filters = {} if filters is None else filters
    columns = sorted(COLUMNS) if columns is None else columns
    if not isinstance(filters, dict) or filters.keys() - FILTERS:
        raise ValueError('unsupported asset filter')
    for key, values in filters.items():
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError('bounded filter arrays required')
        for item in values:
            if key in ('active', 'rural', 'requires_evidence_review'):
                if type(item) is not bool:
                    raise ValueError('boolean filter required')
            elif not isinstance(item, str):
                raise ValueError('string filter required')
    if not isinstance(columns, list) or not columns or len(columns) != len(set(columns)) or set(columns) - COLUMNS:
        raise ValueError('unsupported projection')
    integer(limit, 'limit', 1, 1000)
    context = digest({'analysis': result['analysis_digest'], 'filters': filters, 'columns': columns})
    rows = [row for row in result['risk']['rows'] if all(row[key] in values for key, values in filters.items())]
    rows.sort(key=lambda row: row['asset'])
    offset = 0
    if cursor is not None:
        decoded = decode(cursor)
        if decoded['context'] != context:
            raise ValueError('cursor belongs to another analysis or query')
        offset = integer(decoded['offset'], 'cursor.offset', 0, len(rows))
    page = rows[offset:offset + limit]
    next_offset = offset + len(page)
    return {'rows': [{key: row[key] for key in columns} for row in page], 'total': len(rows), 'next_cursor': encode({'context': context, 'offset': next_offset}) if next_offset < len(rows) else None}


def asset_detail(catalog, result, key):
    if key not in catalog.assets:
        raise KeyError(key)
    scored = next(row for row in result['risk']['rows'] if row['asset'] == key)
    evidence = next(row for row in result['risk']['evidence']['rows'] if row['asset'] == key)
    candidates = [row for row in result['plan']['benefits'] if row['asset'] == key]
    selected = [row['proposal'] for row in candidates if row['proposal'] in result['plan']['selected']]
    return {'asset': catalog.assets[key], 'lineage': lineage(catalog, key), 'risk': scored, 'evidence': evidence, 'proposals': candidates, 'selected': selected}


def report(catalog, result, kind, options=None):
    options = {} if options is None else options
    risk, plan = result['risk'], result['plan']
    if kind in ('optimality-gap', 'resource-slack'):
        from funding.certificate import gap, resource_slack
        fields(options, ())
        return (gap if kind == 'optimality-gap' else resource_slack)(catalog, risk, plan)
    if kind == 'scenarios':
        from decisions.scenarios import evaluate
        fields(options, ('definitions',), ('maximum_nodes',))
        return evaluate(catalog, result['as_of'], result['known_at'], **options)
    if kind == 'risk-series':
        from risk_model.trends import series
        fields(options, ('dates',))
        return series(catalog, options['dates'], result['known_at'])
    if kind == 'evidence-aging':
        from risk_model.trends import evidence_aging
        fields(options, ('horizons',))
        return evidence_aging(catalog, result['as_of'], result['known_at'], options['horizons'])
    if kind == 'evidence-request':
        from equipment_domain.quality import evidence_request
        fields(options, ('id',))
        return evidence_request(catalog, risk, options['id'])
    if kind == 'quality-groups':
        from equipment_domain.quality import group
        fields(options, (), ('dimension',))
        return group(result['quality'], **options)
    if kind == 'assets':
        fields(options, (), ('filters', 'columns', 'limit', 'cursor'))
        return assets(result, **options)
    if kind == 'asset':
        fields(options, ('id',))
        return asset_detail(catalog, result, options['id'])
    if kind == 'risk-summary':
        fields(options, (), ('dimension',))
        return summary(risk, **options)
    if kind == 'public':
        fields(options, (), ('dimension', 'minimum_sites'))
        return public_summary(risk, **options)
    if kind == 'investment':
        fields(options, (), ('dimension',))
        return investment_distribution(catalog, risk, plan, **options)
    if kind == 'tradeoffs':
        fields(options, (), ('maximum_nodes',))
        return forced_tradeoffs(catalog, risk, plan, **options)
    if kind == 'budget-frontier':
        fields(options, ('period_id', 'budgets'), ('maximum_nodes',))
        return budget_frontier(catalog, risk, **options)
    if kind == 'uncertainty':
        fields(options, (), ('maximum_nodes',))
        return uncertainty_portfolios(catalog, risk, **options)
    if kind == 'weights':
        fields(options, ('source', 'target', 'amounts'), ('maximum_nodes',))
        return weight_shift(catalog, risk, **options)
    if kind == 'crew-frontier':
        fields(options, ('period_id', 'capacities'))
        return crew_frontier(catalog, risk, **options)
    if kind == 'calibration':
        fields(options, ('outcomes',))
        return calibration_check(risk, options['outcomes'])
    if kind == 'maintenance':
        fields(options, ('start', 'end'))
        return history(catalog, options['start'], options['end'], result['known_at'])
    if kind == 'lineage':
        fields(options, ('id',))
        return lineage(catalog, options['id'])
    if kind == 'dominance':
        fields(options, ())
        return dominance(catalog, risk, plan['mode'])
    if kind == 'objective':
        fields(options, ())
        return objective_attribution(catalog, risk, plan)
    simple = {'quality': 'quality', 'calendar': 'candidate_calendar', 'calendar-inventory': 'calendar_inventory', 'lots': 'procurement_lots', 'risk': 'risk', 'plan': 'plan', 'exclusions': 'exclusion_reasons', 'inventory': 'operating_inventory', 'component-warnings': 'component_warnings', 'overlaps': 'maintenance_overlaps', 'ranking-stability': 'ranking_stability', 'inspection-priority': 'inspection_priority'}
    if kind in simple:
        fields(options, ())
        return result[simple[kind]]
    raise ValueError('unknown report kind')


def csv_report(catalog, result, kind):
    import csv
    import io
    if kind == 'risks':
        header = ['asset', 'site', 'class', 'band', 'active', 'central_numerator', 'central_denominator', 'review_required']
        rows = [[row['asset'], row['site'], row['class'], row['band'], row['active'], row['central']['numerator'], row['central']['denominator'], row['requires_evidence_review']] for row in result['risk']['rows']]
    elif kind == 'selected':
        header = ['proposal', 'asset', 'period', 'cost', 'crew_days', 'outage_days']
        rows = [[catalog.proposals[key][column] if column != 'proposal' else key for column in header] for key in result['plan']['selected']]
    elif kind == 'periods':
        if result['plan']['status'] == 'infeasible':
            raise ValueError('period certificate unavailable for infeasible plan')
        header = ['period', 'cost_cents', 'budget_cents', 'unspent_cents', 'crew_days', 'crew_capacity']
        rows = [[row[key] for key in header] for row in result['plan']['constraint_check']['periods']]
    else:
        raise ValueError('unknown CSV report')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue()
