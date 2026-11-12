from collections import defaultdict
from equipment_domain.catalog import Catalog
from equipment_domain.values import day, digest, exact, fields
from equipment_domain.change_impact import compare as source_changes
from risk_model.scoring import score, value
from funding.optimizer import benefits, objective
from funding.constraints import evaluate


def reassess(workspace, run_id, source_id, as_of, known_at):
    original = workspace.result(run_id)
    prior_catalog = workspace.catalog(run_id)
    current = workspace.source(source_id)
    catalog = Catalog(current['document'])
    old = original['result']
    if day(as_of, 'as_of') < day(old['as_of'], 'original.as_of'):
        raise ValueError('monitor cannot move business time backwards')
    if day(known_at, 'known_at') < day(old['known_at'], 'original.known_at'):
        raise ValueError('monitor cannot discard previously available knowledge')
    changes = source_changes(prior_catalog, catalog)
    scored = score(catalog, as_of, known_at)
    existing = set(old['plan']['selected'])
    missing = sorted(existing - catalog.proposals.keys())
    selected = sorted(existing & catalog.proposals.keys())
    constraint = evaluate(catalog, scored, selected)
    old_risk = {row['asset']: row for row in old['risk']['rows']}
    new_risk = {row['asset']: row for row in scored['rows']}
    warnings = []
    changed_selected = []
    for key in selected:
        left = prior_catalog.proposals[key]
        right = catalog.proposals[key]
        if left != right:
            changed_selected.append(key)
            warnings.append({'code': 'selected_proposal_terms_changed', 'proposal': key, 'before': digest(left), 'after': digest(right)})
        risk = new_risk[right['asset']]
        if risk['requires_evidence_review']:
            warnings.append({'code': 'selected_asset_evidence_requires_review', 'proposal': key, 'asset': right['asset'], 'checks': risk['unresolved_checks']})
        previous = old_risk.get(left['asset'])
        if previous and value(risk['high']) > value(previous['high']):
            warnings.append({'code': 'selected_asset_upper_risk_increased', 'proposal': key, 'asset': right['asset'], 'before': previous['high'], 'after': risk['high']})
    for key in missing:
        warnings.append({'code': 'selected_proposal_missing', 'proposal': key})
    if changes['policy_changed']:
        warnings.append({'code': 'scoring_policy_changed', 'before': digest(prior_catalog.policy), 'after': digest(catalog.policy)})
    for violation in constraint['violations']:
        warnings.append({'code': 'current_constraint_violation', 'violation': violation})
    benefit = benefits(catalog, scored, old['plan']['mode'])
    calculated = objective(benefit, selected)
    body = {
        'original_run': run_id,
        'original_analysis_digest': old['analysis_digest'],
        'current_source': source_id,
        'current_revision': current['revision'],
        'current_input_digest': catalog.fingerprint,
        'as_of': as_of,
        'known_at': known_at,
        'source_changes': changes,
        'selected_proposals_missing': missing,
        'selected_terms_changed': changed_selected,
        'original_selection_still_feasible': not missing and constraint['feasible'],
        'current_constraint_check': constraint,
        'current_selection_exposure_reduction': exact(calculated[0]),
        'current_selection_cost_cents': -calculated[1],
        'warnings': warnings,
        'original_snapshot_modified': False,
        'original_approval_not_extended_to_current_terms': True,
        'monitor_does_not_reoptimize_or_execute_procurement': True,
    }
    return {**body, 'monitor_digest': digest(body)}


def grouped(report, dimension='code'):
    if dimension not in ('code', 'asset', 'proposal'):
        raise ValueError('unsupported monitor grouping')
    groups = defaultdict(list)
    for warning in report['warnings']:
        groups[warning.get(dimension)].append(warning)
    output = []
    for key, records in sorted(groups.items(), key=lambda item: str(item[0])):
        output.append({dimension: key, 'warnings': len(records), 'warning_digests': sorted(digest(row) for row in records)})
    return {'dimension': dimension, 'rows': output}


def verify(report):
    fields(report, (
        'original_run', 'original_analysis_digest', 'current_source',
        'current_revision', 'current_input_digest', 'as_of', 'known_at',
        'source_changes', 'selected_proposals_missing', 'selected_terms_changed',
        'original_selection_still_feasible', 'current_constraint_check',
        'current_selection_exposure_reduction', 'current_selection_cost_cents',
        'warnings', 'original_snapshot_modified',
        'original_approval_not_extended_to_current_terms',
        'monitor_does_not_reoptimize_or_execute_procurement', 'monitor_digest',
    ))
    if digest({key: value for key, value in report.items() if key != 'monitor_digest'}) != report['monitor_digest']:
        raise ValueError('monitor checksum mismatch')
    if report['original_snapshot_modified']:
        raise ValueError('monitor cannot alter original snapshot')
    expected = not report['selected_proposals_missing'] and report['current_constraint_check']['feasible']
    if bool(expected) != report['original_selection_still_feasible']:
        raise ValueError('monitor feasibility summary mismatch')
    if report['current_selection_cost_cents'] != report['current_constraint_check']['total_cost_cents']:
        raise ValueError('monitor financial summary mismatch')
    return {'valid': True, 'warnings': len(report['warnings'])}
