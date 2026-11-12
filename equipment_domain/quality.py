from collections import defaultdict
from equipment_domain.values import day, digest, amount
from equipment_domain.evidence import resolve, lineage, active
from equipment_domain.maintenance import overlaps, component_warnings
from funding.constraints import closure, evaluate, incompatibilities


SEVERITY = {'error': 0, 'review': 1, 'information': 2}


def inspect(catalog, scored):
    findings = []
    evidence = scored['evidence']
    for row in evidence['rows']:
        if not row['active']:
            continue
        for check in row['checks']:
            if check['status'] != 'current':
                findings.append({
                    'severity': 'review',
                    'code': 'inspection_' + check['status'],
                    'asset': row['asset'],
                    'check': check['check'],
                    'evidence': check['evidence_ids'],
                    'repair': check['repair_evidence'],
                })
    for overlap in overlaps(catalog, scored['known_at'])['rows']:
        findings.append({'severity': 'review', 'code': 'overlapping_maintenance', **overlap})
    for warning in component_warnings(catalog, scored['as_of'])['rows']:
        findings.append({'severity': 'review', **warning})
    seen = defaultdict(list)
    for row in catalog.inspections.values():
        if day(row['recorded'], 'recorded') > day(scored['known_at'], 'known_at'):
            continue
        key = row['asset'], row['performed'], digest(row['checks'])
        seen[key].append(row['id'])
    for (asset, performed, signature), records in seen.items():
        if len(records) > 1:
            findings.append({'severity': 'information', 'code': 'duplicate_inspection_content', 'asset': asset, 'performed': performed, 'records': sorted(records), 'check_digest': signature})
    for key, proposal in catalog.proposals.items():
        required = closure(catalog, {key})
        check = evaluate(catalog, scored, required, require_mandatory=False)
        if not check['feasible']:
            findings.append({'severity': 'review', 'code': 'candidate_closure_infeasible', 'proposal': key, 'closure': sorted(required), 'violations': check['violations']})
        if proposal['action'] == 'inspect' and proposal['reduction']['numerator']:
            findings.append({'severity': 'review', 'code': 'inspection_declares_physical_risk_reduction', 'proposal': key, 'asset': proposal['asset'], 'rule': 'inspection_alone_does_not_repair_device_assumption_requires_review'})
        if amount(proposal['cost'], 'cost') == 0 and proposal['reduction']['numerator']:
            findings.append({'severity': 'information', 'code': 'zero_cost_risk_reduction_assumption', 'proposal': key})
    mandatory = {key for key, row in catalog.proposals.items() if row['mandatory']}
    if mandatory:
        required = closure(catalog, mandatory)
        check = evaluate(catalog, scored, required)
        if not check['feasible']:
            findings.append({'severity': 'error', 'code': 'mandatory_portfolio_infeasible', 'proposals': sorted(required), 'violations': check['violations']})
    for site_id, site in catalog.sites.items():
        if site['users'] == 0:
            findings.append({'severity': 'information', 'code': 'zero_user_exposure_uses_one_for_ranking', 'site': site_id})
    findings.sort(key=lambda row: (SEVERITY[row['severity']], row['code'], row.get('asset', ''), row.get('proposal', ''), digest(row)))
    return {
        'findings': findings,
        'counts': {level: sum(row['severity'] == level for row in findings) for level in SEVERITY},
        'source_digest': catalog.fingerprint,
        'scored_digest': scored['score_digest'],
        'quality_findings_do_not_silently_modify_input': True,
    }


def group(report, dimension='code'):
    if dimension not in ('code', 'severity', 'asset', 'proposal'):
        raise ValueError('unsupported quality grouping')
    groups = defaultdict(list)
    for finding in report['findings']:
        groups[finding.get(dimension)].append(finding)
    output = []
    for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
        output.append({dimension: key, 'findings': len(rows), 'severity_counts': {level: sum(row['severity'] == level for row in rows) for level in SEVERITY}, 'finding_digests': sorted(digest(row) for row in rows)})
    return {'dimension': dimension, 'rows': output}


def compare(before, after):
    old = {digest(row): row for row in before['findings']}
    new = {digest(row): row for row in after['findings']}
    return {
        'new': [new[key] for key in sorted(new.keys() - old.keys())],
        'absent': [old[key] for key in sorted(old.keys() - new.keys())],
        'unchanged': len(old.keys() & new.keys()),
        'absence_is_not_automatic_resolution_without_review': True,
    }


def evidence_request(catalog, scored, asset_id):
    if asset_id not in catalog.assets:
        raise KeyError(asset_id)
    row = next(row for row in scored['evidence']['rows'] if row['asset'] == asset_id)
    requests = []
    for check in row['checks']:
        if check['status'] == 'current':
            continue
        requests.append({
            'check': check['check'],
            'reason': check['status'],
            'last_evidence': check['evidence_ids'],
            'repair_to_reinspect': check['repair_evidence'],
            'severity_scale': {'minimum': 0, 'maximum': 4},
            'requires_independent_inspector': check['conflicting'],
        })
    return {'asset': asset_id, 'as_of': scored['as_of'], 'requests': requests, 'read_only_no_work_order_created': True}
