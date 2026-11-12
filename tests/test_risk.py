import copy
from datetime import date
from fractions import Fraction
import json
from pathlib import Path
import unittest
from equipment_domain.catalog import Catalog
from equipment_domain.values import Invalid, amount, money, day, ratio, exact, digest
from equipment_domain.evidence import resolve, lineage, evidence_delta
from equipment_domain.maintenance import history, overlaps, operating_inventory, component_warnings
from equipment_domain.quality import inspect, group, evidence_request
from risk_model.policy import validate, contribution, factor_bounds, variant, band
from risk_model.scoring import score, verify, value, priority_stability, inspection_value
from risk_model.portfolio import summary, public_summary, calibration_check
from risk_model.trends import series, decompose, transitions, evidence_aging


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/equipment.json').read_text())
        self.catalog = Catalog(self.data)
        self.as_of = '2026-11-12'
        self.known = '2026-11-13'
        self.scored = score(self.catalog, self.as_of, self.known)

    def risk(self, key='a1', scored=None):
        return next(row for row in (scored or self.scored)['rows'] if row['asset'] == key)

    def test_money_is_exact_and_rejects_ambiguous_input(self):
        self.assertEqual(amount('0.01', 'money'), 1)
        self.assertEqual(amount('123.45', 'money'), 12345)
        self.assertEqual(money(12345), '123.45')
        self.assertEqual(money(0), '0.00')
        for invalid in ('01.00', '1', '1.0', '-1.00', '1.001', 'NaN', '1e2', 1, True):
            with self.subTest(value=invalid):
                with self.assertRaises(Invalid):
                    amount(invalid, 'money')
        self.assertEqual(ratio({'numerator': 1, 'denominator': 3}, 'r'), Fraction(1, 3))
        with self.assertRaises(Invalid):
            ratio({'numerator': 2, 'denominator': 1}, 'r')
        with self.assertRaises(Invalid):
            ratio({'numerator': 1, 'denominator': 0}, 'r')

    def test_dates_and_strict_catalog_schema(self):
        self.assertEqual(day('2024-02-29', 'd'), date(2024, 2, 29))
        for invalid in ('2026-02-29', '2026-1-01', '1999-12-31', '2101-01-01', 1):
            with self.subTest(value=invalid):
                with self.assertRaises(Invalid):
                    day(invalid, 'd')
        for key, value in [('version', True), ('version', 2), ('assets', {}), ('classes', None)]:
            data = copy.deepcopy(self.data)
            data[key] = value
            with self.assertRaises(ValueError):
                Catalog(data)
        data = copy.deepcopy(self.data)
        data['extra'] = 'ignored?'
        with self.assertRaises(Invalid):
            Catalog(data)

    def test_catalog_owns_input_and_export(self):
        original = self.catalog.fingerprint
        self.data['assets'][0]['serial'] = 'changed'
        self.assertEqual(self.catalog.assets['a1']['serial'], 'SN-a1')
        self.assertEqual(self.catalog.fingerprint, original)
        exported = self.catalog.export()
        exported['sites'][0]['users'] = 999
        self.assertEqual(self.catalog.sites['s1']['users'], 100)
        self.assertEqual(digest(self.catalog.export()), original)
        self.assertNotEqual(digest(exported), original)

    def test_hierarchy_cycles_and_cross_site_components(self):
        data = copy.deepcopy(self.data)
        data['assets'][0]['parent'] = 'a2'
        data['assets'][1]['parent'] = 'a1'
        with self.assertRaises(ValueError):
            Catalog(data)
        data['assets'][1]['parent'] = None
        catalog = Catalog(data)
        self.assertEqual(lineage(catalog, 'a1')['ancestors'], ['a2'])
        self.assertEqual(lineage(catalog, 'a2')['descendants'], ['a1'])
        self.assertEqual(lineage(catalog, 'a2')['direct_children'], ['a1'])
        data['assets'][0]['parent'] = 'a3'
        with self.assertRaises(ValueError):
            Catalog(data)
        with self.assertRaises(KeyError):
            lineage(catalog, 'unknown')

    def test_duplicate_serial_and_invalid_references(self):
        for table, key, value in [('assets', 'serial', 'SN-a2'), ('assets', 'site', 'unknown'), ('assets', 'class', 'unknown'), ('assets', 'parent', 'a1'), ('assets', 'utilization_pct', 101), ('classes', 'design_life_days', 0), ('inspections', 'asset', 'unknown'), ('proposals', 'period', 'unknown')]:
            with self.subTest(table=table, key=key):
                data = copy.deepcopy(self.data)
                data[table][0][key] = value
                with self.assertRaises(ValueError):
                    Catalog(data)
        data = copy.deepcopy(self.data)
        data['classes'][0]['checks'] = []
        with self.assertRaises(ValueError):
            Catalog(data)
        data = copy.deepcopy(self.data)
        data['sites'][0]['minimum_operating'] = {'unknown': 1}
        with self.assertRaises(ValueError):
            Catalog(data)

    def test_current_checks_and_missing_uncertainty(self):
        evidence = self.scored['evidence']
        first = next(row for row in evidence['rows'] if row['asset'] == 'a1')
        missing = next(row for row in evidence['rows'] if row['asset'] == 'a4')
        self.assertEqual(first['current_checks'], 2)
        self.assertEqual(first['required_checks'], 2)
        self.assertTrue(all(row['status'] == 'current' for row in first['checks']))
        self.assertEqual(missing['current_checks'], 0)
        self.assertTrue(all(row['status'] == 'missing' for row in missing['checks']))
        self.assertFalse(self.risk('a1')['requires_evidence_review'])
        self.assertTrue(self.risk('a4')['requires_evidence_review'])
        self.assertLess(value(self.risk('a4')['low']), value(self.risk('a4')['central']))
        self.assertLess(value(self.risk('a4')['central']), value(self.risk('a4')['high']))
        self.assertEqual(self.risk('a1')['low'], self.risk('a1')['high'])
        self.assertTrue(verify(self.catalog, self.scored)['valid'])

    def test_score_matches_independent_fraction_calculation(self):
        age = (date(2026, 11, 12) - date(2018, 1, 1)).days
        age_factor = min(Fraction(100), Fraction(age * 100, 3650))
        expected = age_factor * Fraction(20, 100) + 100 * Fraction(40, 100) + 80 * Fraction(15, 100) + 80 * Fraction(15, 100) + 50 * Fraction(10, 100)
        self.assertEqual(value(self.risk()['central']), expected)
        self.assertEqual(sum(value(row['contribution']) for row in self.risk()['factors']), expected)
        self.assertEqual(self.risk()['band'], 'critical')
        self.assertEqual(self.scored['ranking'][0], 'a1')
        self.assertEqual(self.risk()['priority_rank'], 1)
        self.assertEqual(value(self.risk()['coverage_pct']), 100)
        self.assertEqual(self.risk()['users_exposed'], 100)

    def test_policy_sum_thresholds_and_missing_bounds(self):
        self.assertTrue(validate(self.catalog.policy))
        for path, value in [('weights', {'age': 100, 'condition': 1, 'utilization': 0, 'criticality': 0, 'redundancy': 0}), ('thresholds', {'watch': 30, 'urgent': 30, 'critical': 80}), ('missing_condition', {'low': 80, 'central': 50, 'high': 100}), ('minimum_evidence_pct', True)]:
            data = copy.deepcopy(self.catalog.policy)
            data[path] = value
            with self.assertRaises(ValueError):
                validate(data)
        revised = variant(self.catalog.policy, thresholds={'watch': 20, 'urgent': 40, 'critical': 90})
        self.assertEqual(revised['version'], 2)
        self.assertEqual(self.catalog.policy['version'], 1)
        self.assertEqual(band(30, self.catalog.policy), 'watch')
        self.assertEqual(band(60, self.catalog.policy), 'urgent')
        self.assertEqual(band(80, self.catalog.policy), 'critical')
        self.assertEqual(band(0, self.catalog.policy), 'routine')

    def test_late_inspection_visible_only_at_knowledge_cutoff(self):
        data = copy.deepcopy(self.data)
        data['inspections'].append({'id': 'late', 'asset': 'a4', 'performed': '2026-11-10', 'recorded': '2026-11-15', 'inspector': 'auditor', 'checks': {'electrical': 0, 'mechanical': 0}})
        catalog = Catalog(data)
        before = resolve(catalog, self.as_of, self.known)
        after = resolve(catalog, self.as_of, '2026-11-16')
        old = next(row for row in before['rows'] if row['asset'] == 'a4')
        new = next(row for row in after['rows'] if row['asset'] == 'a4')
        self.assertEqual(old['current_checks'], 0)
        self.assertEqual(new['current_checks'], 2)
        self.assertIn({'kind': 'inspection', 'id': 'late', 'reason': 'not_recorded_yet'}, before['excluded'])
        delta = evidence_delta(before, after)
        self.assertEqual([row['asset'] for row in delta['changes']], ['a4'])
        self.assertEqual(delta['changes'][0]['before_current'], 0)
        self.assertEqual(delta['changes'][0]['after_current'], 2)
        with self.assertRaises(ValueError):
            evidence_delta(after, before)
        with self.assertRaises(ValueError):
            resolve(catalog, self.as_of, '2026-11-11')

    def test_same_day_conflict_is_conservative_and_requires_review(self):
        data = copy.deepcopy(self.data)
        data['inspections'].append({**data['inspections'][0], 'id': 'conflict', 'checks': {'electrical': 0, 'mechanical': 4}})
        catalog = Catalog(data)
        scored = score(catalog, self.as_of, self.known)
        evidence = next(row for row in scored['evidence']['rows'] if row['asset'] == 'a1')
        electrical = next(row for row in evidence['checks'] if row['check'] == 'electrical')
        self.assertEqual(electrical['status'], 'conflicting_same_day_evidence')
        self.assertEqual(electrical['severity'], 4)
        self.assertEqual(electrical['evidence_ids'], ['conflict', 'i-a1'])
        self.assertTrue(self.risk('a1', scored)['requires_evidence_review'])
        self.assertEqual(self.risk('a1', scored)['central'], self.risk()['central'])
        self.assertLess(value(self.risk('a1', scored)['low']), value(self.risk()['low']))
        self.assertEqual(evidence['current_checks'], 1)

    def test_inspection_expiry_boundary_and_repair_requires_reinspection(self):
        data = copy.deepcopy(self.data)
        data['classes'][0]['inspection_valid_days'] = 11
        current = resolve(Catalog(data), self.as_of, self.known)
        self.assertEqual(current['rows'][0]['current_checks'], 2)
        expired = resolve(Catalog(data), '2026-11-13', self.known)
        self.assertEqual(expired['rows'][0]['current_checks'], 0)
        self.assertTrue(all(row['expired'] for row in expired['rows'][0]['checks']))
        data['maintenance'] = [{'id': 'repair', 'asset': 'a1', 'start': '2026-11-05', 'end': '2026-11-06', 'recorded': '2026-11-07', 'kind': 'repair', 'cost': '10.00', 'restores_checks': ['electrical']}]
        evidence = resolve(Catalog(data), self.as_of, self.known)['rows'][0]
        electrical = next(row for row in evidence['checks'] if row['check'] == 'electrical')
        self.assertEqual(electrical['status'], 'requires_reinspection_after_repair')
        self.assertIsNone(electrical['severity'])
        self.assertEqual(electrical['repair_evidence'], 'repair')
        self.assertEqual(evidence['current_checks'], 1)

    def test_maintenance_interval_union_and_full_cost_rule(self):
        data = copy.deepcopy(self.data)
        data['maintenance'] = [
            {'id': 'm1', 'asset': 'a1', 'start': '2026-11-01', 'end': '2026-11-05', 'recorded': '2026-11-06', 'kind': 'repair', 'cost': '10.00', 'restores_checks': []},
            {'id': 'm2', 'asset': 'a1', 'start': '2026-11-03', 'end': '2026-11-07', 'recorded': '2026-11-08', 'kind': 'preventive', 'cost': '20.00', 'restores_checks': []},
        ]
        catalog = Catalog(data)
        result = history(catalog, '2026-11-02', '2026-11-10', self.known)
        row = result['rows'][0]
        self.assertEqual(row['union_outage_days'], 5)
        self.assertEqual(row['in_service_days'], 8)
        self.assertEqual(row['available_days'], 3)
        self.assertEqual(row['whole_record_cost_cents'], 3000)
        self.assertEqual(row['availability'], {'numerator': 3, 'denominator': 8})
        self.assertEqual(overlaps(catalog, self.known)['rows'][0]['overlap_days'], 2)
        self.assertEqual(row['records'], ['m1', 'm2'])
        self.assertEqual(overlaps(catalog, '2026-11-06')['rows'], [])
        with self.assertRaises(ValueError):
            history(catalog, self.as_of, self.as_of, self.known)

    def test_retirement_half_open_and_component_warning(self):
        data = copy.deepcopy(self.data)
        data['assets'][1]['parent'] = 'a1'
        data['assets'][0]['retired'] = self.as_of
        catalog = Catalog(data)
        scored = score(catalog, self.as_of, self.known)
        self.assertFalse(self.risk('a1', scored)['active'])
        self.assertNotIn('a1', scored['ranking'])
        inventory = operating_inventory(catalog, self.as_of)
        self.assertEqual(inventory['rows'][0]['active'], 1)
        self.assertEqual(inventory['rows'][0]['retired'], 1)
        warnings = component_warnings(catalog, self.as_of)
        self.assertEqual(warnings['rows'], [{'asset': 'a2', 'inactive_ancestors': ['a1'], 'code': 'active_component_under_inactive_parent'}])
        self.assertEqual(sum(row['assets'] for row in summary(scored)['rows']), 3)

    def test_risk_summary_publication_and_uncertainty_priorities(self):
        report = summary(self.scored)
        self.assertEqual(sum(row['assets'] for row in report['rows']), 4)
        total = sum(value(row['risk_total']) for row in report['rows'])
        self.assertEqual(total, sum(value(row['central']) for row in self.scored['rows']))
        packet = public_summary(self.scored, minimum_sites=2)
        self.assertTrue(all(row['suppressed'] for row in packet['rows']))
        self.assertFalse(packet['contains_asset_or_site_ids'])
        self.assertTrue(packet['total_omitted'])
        open_packet = public_summary(self.scored, minimum_sites=1)
        self.assertFalse(any(row['suppressed'] for row in open_packet['rows']))
        self.assertEqual(inspection_value(self.scored)['rows'][0]['asset'], 'a4')
        stability = priority_stability(self.scored)
        self.assertEqual(len(stability['rows']), 4)
        for row in stability['rows']:
            self.assertLessEqual(row['best_possible_rank'], row['central_rank'])
            self.assertGreaterEqual(row['worst_possible_rank'], row['central_rank'])
        with self.assertRaises(ValueError):
            public_summary(self.scored, dimension='site')

    def test_calibration_keeps_ordinal_index_distinct_from_probability(self):
        result = calibration_check(self.scored, [{'asset': 'a1', 'failed': True}, {'asset': 'a2', 'failed': False}])
        self.assertEqual(sum(row['observed_assets'] for row in result['rows']), 2)
        self.assertEqual(sum(row['failures'] for row in result['rows']), 1)
        self.assertEqual(result['unobserved_assets'], ['a3', 'a4'])
        self.assertTrue(result['risk_score_is_not_probability_no_brier_score_reported'])
        with self.assertRaises(ValueError):
            calibration_check(self.scored, [{'asset': 'a1', 'failed': 1}])
        with self.assertRaises(ValueError):
            calibration_check(self.scored, [{'asset': 'a1', 'failed': True}, {'asset': 'a1', 'failed': False}])
        with self.assertRaises(ValueError):
            calibration_check(self.scored, [{'asset': 'unknown', 'failed': True}])

    def test_trends_decompose_changes_and_preserve_fixed_knowledge(self):
        early = score(self.catalog, '2026-11-11', self.known)
        later = self.scored
        result = decompose(early, later)
        self.assertEqual(len(result['rows']), 4)
        for row in result['rows']:
            self.assertEqual(sum(value(factor['contribution_change']) for factor in row['factors']), value(row['central_change']))
            self.assertFalse(row['evidence_changed'])
        matrix = transitions(early, later)
        self.assertEqual(sum(row['assets'] for row in matrix['rows']), 4)
        trace = series(self.catalog, ['2026-11-11', self.as_of], self.known)
        self.assertEqual(len(trace['rows']), 4)
        self.assertEqual(len(trace['rows'][0]['observations']), 2)
        aging = evidence_aging(self.catalog, self.as_of, self.known, [0, 1])
        self.assertEqual([row['active_assets'] for row in aging['rows']], [4, 4])
        with self.assertRaises(ValueError):
            series(self.catalog, [self.as_of, '2026-11-11'], self.known)
        with self.assertRaises(ValueError):
            evidence_aging(self.catalog, self.as_of, self.known, [2])

    def test_evidence_identity_changes_on_expiry_but_not_daily_age(self):
        data = copy.deepcopy(self.data)
        data['classes'][0]['inspection_valid_days'] = 11
        catalog = Catalog(data)
        early = score(catalog, '2026-11-11', self.known)
        current = score(catalog, '2026-11-12', self.known)
        expired = score(catalog, '2026-11-13', self.known)
        before = next(row for row in early['evidence']['rows'] if row['asset'] == 'a1')
        after = next(row for row in current['evidence']['rows'] if row['asset'] == 'a1')
        self.assertNotEqual(before['checks'][0]['inspection_age_days'], after['checks'][0]['inspection_age_days'])
        self.assertEqual(before['evidence_digest'], after['evidence_digest'])
        self.assertTrue(all(not row['evidence_changed'] for row in decompose(early, current)['rows']))
        changes = {row['asset']: row for row in decompose(current, expired)['rows']}
        self.assertTrue(changes['a1']['evidence_changed'])
        self.assertFalse(changes['a4']['evidence_changed'])
        self.assertEqual(value(changes['a1']['central_change']), Fraction(20, 3650) - 20)
        risk = next(row for row in expired['rows'] if row['asset'] == 'a1')
        self.assertTrue(risk['requires_evidence_review'])
        self.assertLess(value(risk['low']), value(risk['high']))

    def test_quality_report_identifies_missing_evidence_without_mutation(self):
        before = self.catalog.fingerprint
        report = inspect(self.catalog, self.scored)
        missing = [row for row in report['findings'] if row['code'] == 'inspection_missing']
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(row['asset'] == 'a4' for row in missing))
        grouped = group(report)
        self.assertEqual(sum(row['findings'] for row in grouped['rows']), len(report['findings']))
        requests = evidence_request(self.catalog, self.scored, 'a4')
        self.assertEqual([row['check'] for row in requests['requests']], ['electrical', 'mechanical'])
        self.assertEqual(evidence_request(self.catalog, self.scored, 'a1')['requests'], [])
        self.assertEqual(self.catalog.fingerprint, before)
        self.assertEqual(digest(self.catalog.document), before)
        with self.assertRaises(KeyError):
            evidence_request(self.catalog, self.scored, 'missing')


if __name__ == '__main__':
    unittest.main()
