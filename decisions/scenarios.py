from fractions import Fraction
from equipment_domain.catalog import Catalog
from equipment_domain.values import fields, rows, amount, money, integer, ratio, digest, exact, day
from decisions.analysis import analyze, compare
from risk_model.scoring import value


KINDS = {'budget', 'crew', 'cost', 'reduction', 'mandatory', 'utilization', 'evidence'}


def apply(catalog, operations):
    rows(operations, 'operations', 100)
    document = catalog.export()
    tables = {table: {row['id']: row for row in document[table]} for table in ('periods', 'proposals', 'assets', 'inspections')}
    touched = set()
    for operation in operations:
        fields(operation, ('kind', 'id', 'value'))
        kind, key, value = operation['kind'], operation['id'], operation['value']
        if kind not in KINDS or (kind, key) in touched:
            raise ValueError('unknown or duplicate scenario operation')
        touched.add((kind, key))
        if kind in ('budget', 'crew'):
            if key not in tables['periods']:
                raise KeyError(key)
            if kind == 'budget':
                amount(value, 'budget')
                tables['periods'][key]['budget'] = value
            else:
                integer(value, 'crew_days', 0, 100000)
                tables['periods'][key]['crew_days'] = value
        elif kind in ('cost', 'reduction', 'mandatory'):
            if key not in tables['proposals']:
                raise KeyError(key)
            if kind == 'cost':
                amount(value, 'cost')
            elif kind == 'reduction':
                ratio(value, 'reduction')
            elif type(value) is not bool:
                raise ValueError('mandatory scenario requires boolean')
            tables['proposals'][key][kind] = value
        elif kind == 'utilization':
            if key not in tables['assets']:
                raise KeyError(key)
            integer(value, 'utilization_pct', 0, 100)
            tables['assets'][key]['utilization_pct'] = value
        elif kind == 'evidence':
            if key in tables['inspections']:
                raise ValueError('scenario evidence identity already exists')
            if not isinstance(value, dict) or value.get('id') != key:
                raise ValueError('evidence row must preserve operation id')
            document['inspections'].append(value)
            tables['inspections'][key] = value
    return Catalog(document)


def evaluate(catalog, as_of, known_at, definitions, maximum_nodes=100000):
    rows(definitions, 'scenarios', 20)
    base = analyze(catalog, as_of, known_at, maximum_nodes=maximum_nodes)
    names = set()
    output = []
    for definition in definitions:
        fields(definition, ('name', 'operations'))
        from equipment_domain.values import ident
        name = ident(definition['name'], 'scenario.name')
        if name in names:
            raise ValueError('duplicate scenario name')
        names.add(name)
        revised = apply(catalog, definition['operations'])
        result = analyze(revised, as_of, known_at, maximum_nodes=maximum_nodes)
        plan = result['plan']
        output.append({
            'name': name,
            'operations_digest': digest(definition['operations']),
            'input_digest': revised.fingerprint,
            'analysis_digest': result['analysis_digest'],
            'status': plan['status'],
            'selected': plan['selected'],
            'objective': plan.get('objective'),
            'comparison': compare(base, result),
        })
    return {'base_analysis_digest': base['analysis_digest'], 'rows': output, 'each_scenario_independent_from_original_source': True}


def regret(catalog, scored, candidate_plan, reference_plan):
    from funding.constraints import evaluate
    from funding.optimizer import benefits, objective
    if candidate_plan['status'] == 'infeasible' or reference_plan['status'] == 'infeasible':
        raise ValueError('regret requires two feasible plan selections')
    candidate = evaluate(catalog, scored, candidate_plan['selected'])
    reference = evaluate(catalog, scored, reference_plan['selected'])
    if not candidate['feasible'] or not reference['feasible']:
        return {'comparable': False, 'candidate_violations': candidate['violations'], 'reference_violations': reference['violations']}
    benefit = benefits(catalog, scored, 'central')
    first = objective(benefit, candidate_plan['selected'])
    second = objective(benefit, reference_plan['selected'])
    return {
        'comparable': True,
        'candidate_exposure_reduction': exact(first[0]),
        'reference_exposure_reduction': exact(second[0]),
        'reference_minus_candidate': exact(second[0] - first[0]),
        'candidate_cost_cents': -first[1],
        'reference_cost_cents': -second[1],
        'scope': 'both_selections_reevaluated_under_same_current_source_and_central_score',
        'reference_not_assumed_optimal': True,
    }


def decision_matrix(catalog, scored, plans):
    if not isinstance(plans, dict) or not plans or len(plans) > 20:
        raise ValueError('one to twenty named plans required')
    from funding.constraints import evaluate
    from funding.optimizer import benefits, objective
    benefit = benefits(catalog, scored)
    output = []
    for name, plan in sorted(plans.items()):
        if plan['status'] == 'infeasible':
            output.append({'name': name, 'feasible_under_current_constraints': False, 'declared_status': plan['status'], 'violations': plan.get('violations', [])})
            continue
        checked = evaluate(catalog, scored, plan['selected'])
        score = objective(benefit, plan['selected'])
        output.append({
            'name': name,
            'declared_status': plan['status'],
            'feasible_under_current_constraints': checked['feasible'],
            'violations': checked['violations'],
            'selected': plan['selected'],
            'current_exposure_reduction': exact(score[0]),
            'current_cost_cents': -score[1],
            'requires_evidence_review': any(benefit[key]['evidence_review'] for key in plan['selected']),
        })
    return {'rows': output, 'comparison_basis': 'same_catalog_constraints_and_central_risk'}
