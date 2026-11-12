import copy
from fractions import Fraction
from equipment_domain.values import fields, integer, ratio, exact, digest


FACTORS = ('age', 'condition', 'utilization', 'criticality', 'redundancy')


def validate(policy):
    fields(policy, ('version', 'weights', 'missing_condition', 'thresholds', 'minimum_evidence_pct'))
    integer(policy['version'], 'policy.version', 1, 1000000)
    fields(policy['weights'], FACTORS)
    weights = []
    for factor in FACTORS:
        weights.append(integer(policy['weights'][factor], 'policy.weights.' + factor, 0, 100))
    if sum(weights) != 100:
        raise ValueError('risk weights must sum to 100')
    fields(policy['missing_condition'], ('low', 'central', 'high'))
    missing = policy['missing_condition']
    for key in missing:
        integer(missing[key], 'missing_condition.' + key, 0, 100)
    if not missing['low'] <= missing['central'] <= missing['high']:
        raise ValueError('missing condition bounds must enclose central estimate')
    fields(policy['thresholds'], ('watch', 'urgent', 'critical'))
    limits = policy['thresholds']
    for key in limits:
        integer(limits[key], 'threshold.' + key, 0, 100)
    if not limits['watch'] < limits['urgent'] < limits['critical']:
        raise ValueError('risk band thresholds must strictly increase')
    integer(policy['minimum_evidence_pct'], 'minimum_evidence_pct', 0, 100)
    return True


def band(value, policy):
    validate(policy)
    value = Fraction(value)
    if not 0 <= value <= 100:
        raise ValueError('risk score outside 0..100')
    for name in ('critical', 'urgent', 'watch'):
        if value >= policy['thresholds'][name]:
            return name
    return 'routine'


def variant(policy, weights=None, missing=None, thresholds=None):
    result = copy.deepcopy(policy)
    result['version'] += 1
    if weights is not None:
        result['weights'] = copy.deepcopy(weights)
    if missing is not None:
        result['missing_condition'] = copy.deepcopy(missing)
    if thresholds is not None:
        result['thresholds'] = copy.deepcopy(thresholds)
    validate(result)
    return result


def contribution(factors, policy):
    validate(policy)
    fields(factors, FACTORS)
    output = []
    total = Fraction(0)
    for factor in FACTORS:
        value = Fraction(factors[factor])
        if not 0 <= value <= 100:
            raise ValueError('factor outside range')
        weight = policy['weights'][factor]
        score = value * Fraction(weight, 100)
        total += score
        output.append({'factor': factor, 'raw': exact(value), 'weight': weight, 'contribution': exact(score)})
    return {'score': exact(total), 'band': band(total, policy), 'factors': output, 'policy_digest': digest(policy)}


def factor_bounds(asset, definition, evidence, policy):
    age = min(Fraction(100), Fraction(evidence['age_days'] * 100, definition['design_life_days']))
    utilization = Fraction(asset['utilization_pct'])
    criticality = Fraction(definition['criticality'] * 10)
    redundancy = Fraction(100, asset['redundancy'] + 1)
    low = []
    central = []
    high = []
    unresolved = []
    for check in evidence['checks']:
        if check['status'] == 'current':
            value = Fraction(check['severity'] * 25)
            low.append(value)
            central.append(value)
            high.append(value)
        elif check['status'] == 'conflicting_same_day_evidence':
            low.append(Fraction(0))
            central.append(Fraction(check['severity'] * 25))
            high.append(Fraction(100))
            unresolved.append(check['check'])
        else:
            low.append(Fraction(policy['missing_condition']['low']))
            central.append(Fraction(policy['missing_condition']['central']))
            high.append(Fraction(policy['missing_condition']['high']))
            unresolved.append(check['check'])
    shared = {'age': age, 'utilization': utilization, 'criticality': criticality, 'redundancy': redundancy}
    return {
        'low': {**shared, 'condition': sum(low, Fraction(0)) / len(low)},
        'central': {**shared, 'condition': sum(central, Fraction(0)) / len(central)},
        'high': {**shared, 'condition': sum(high, Fraction(0)) / len(high)},
        'unresolved_checks': sorted(unresolved),
    }
