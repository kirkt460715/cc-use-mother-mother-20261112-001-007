import copy
from fractions import Fraction
import itertools
import json
from pathlib import Path
import random
import unittest
from equipment_domain.catalog import Catalog
from equipment_domain.values import exact, amount, digest
from risk_model.scoring import score, value
from funding.constraints import evaluate, closure, incompatibilities, dependency_order
from funding.optimizer import optimize, verify, benefits, objective
from funding.explanations import exclusions, forced_tradeoffs, dominance, objective_attribution
from funding.sensitivity import budget_frontier, uncertainty_portfolios, weight_shift, crew_frontier
from funding.certificate import relaxed_bound, gap, resource_slack
from funding.lots import schedule, audit, daily_inventory, lot_summary
from decisions.analysis import analyze, compare
from decisions.scenarios import apply, evaluate as evaluate_scenarios, regret, decision_matrix


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/equipment.json').read_text())
        self.catalog = Catalog(self.data)
        self.scored = score(self.catalog, '2026-11-12', '2026-11-13')
        self.plan = optimize(self.catalog, self.scored)

    def solve(self, data, **options):
        catalog = Catalog(data)
        risk = score(catalog, '2026-11-12', '2026-11-13')
        return catalog, risk, optimize(catalog, risk, **options)

    def test_exact_plan_beats_naive_cheapest_order(self):
        self.assertEqual(self.plan['status'], 'optimal')
        self.assertTrue(self.plan['optimality_proven'])
        self.assertEqual(self.plan['selected'], ['replace-a1'])
        self.assertEqual(self.plan['objective']['cost_cents'], 80000)
        self.assertEqual(self.plan['objective']['outage_cost_cents'], 3000)
        expected = value(next(row for row in self.scored['rows'] if row['asset'] == 'a1')['central']) * 100
        self.assertEqual(value(self.plan['objective']['exposure_reduction']), expected)
        self.assertTrue(verify(self.catalog, self.scored, self.plan, True)['valid'])
        self.assertEqual(self.plan['constraint_check']['total_cost_cents'], 80000)
        self.assertGreater(self.plan['search']['visited_nodes'], 0)

    def test_solver_matches_independent_subset_enumeration(self):
        rng = random.Random(20261112)
        for trial in range(50):
            data = copy.deepcopy(self.data)
            for proposal in data['proposals']:
                proposal['cost'] = f'{rng.randrange(0, 11) * 100}.00'
                proposal['crew_days'] = rng.randrange(0, 7)
                proposal['reduction'] = exact(Fraction(rng.randrange(5), 4))
            data['periods'][0]['budget'] = f'{rng.randrange(3, 15) * 100}.00'
            data['periods'][0]['crew_days'] = rng.randrange(0, 12)
            catalog, risk, actual = self.solve(data)
            benefit = benefits(catalog, risk)
            names = sorted(catalog.proposals)
            best_objective = None
            best_selected = None
            for mask in range(1 << len(names)):
                chosen = [key for index, key in enumerate(names) if mask & (1 << index)]
                if not evaluate(catalog, risk, chosen)['feasible']:
                    continue
                result = objective(benefit, chosen)
                if best_objective is None or result > best_objective or (result == best_objective and tuple(chosen) < tuple(best_selected)):
                    best_objective = result
                    best_selected = chosen
            self.assertEqual(actual['selected'], best_selected, trial)
            self.assertEqual(value(actual['objective']['exposure_reduction']), best_objective[0], trial)
            self.assertEqual(actual['objective']['cost_cents'], -best_objective[1], trial)
            self.assertTrue(actual['optimality_proven'])
            upper = gap(catalog, risk, actual)
            self.assertGreaterEqual(value(upper['upper_bound']), best_objective[0])

    def test_requirement_closure_and_exclusion_constraints(self):
        data = copy.deepcopy(self.data)
        data['proposals'][0]['requires'] = ['inspect-a4']
        with self.assertRaisesRegex(ValueError, 'later procurement period'):
            Catalog(data)
        data['proposals'][3]['period'] = 'p1'
        catalog, risk, plan = self.solve(data)
        self.assertEqual(closure(catalog, {'replace-a1'}), {'replace-a1', 'inspect-a4'})
        self.assertLess(dependency_order(catalog).index('inspect-a4'), dependency_order(catalog).index('replace-a1'))
        self.assertIn('inspect-a4', plan['selected'])
        self.assertIn('replace-a1', plan['selected'])
        self.assertEqual(plan['objective']['cost_cents'], 90000)
        missing = evaluate(catalog, risk, ['replace-a1'])
        self.assertFalse(missing['feasible'])
        self.assertIn('requires_missing', [row['code'] for row in missing['violations']])
        data['proposals'][3]['requires'] = ['replace-a1']
        with self.assertRaisesRegex(ValueError, 'cycle'):
            Catalog(data)

    def test_same_asset_alternatives_and_asymmetric_exclusions(self):
        pair = ('replace-a1', 'retrofit-a1')
        self.assertIn(pair, incompatibilities(self.catalog))
        checked = evaluate(self.catalog, self.scored, list(pair))
        self.assertFalse(checked['feasible'])
        self.assertIn('mutually_exclusive', [row['code'] for row in checked['violations']])
        data = copy.deepcopy(self.data)
        data['periods'][0]['budget'] = '2000.00'
        data['proposals'][0]['excludes'] = ['replace-a3']
        catalog, risk, plan = self.solve(data)
        self.assertFalse({'replace-a1', 'replace-a3'} <= set(plan['selected']))
        direct = evaluate(catalog, risk, ['replace-a1', 'replace-a3'])
        self.assertIn('mutually_exclusive', [row['code'] for row in direct['violations']])
        self.assertTrue(verify(catalog, risk, plan, True)['valid'])

    def test_operating_floor_blocks_simultaneous_replacements(self):
        data = copy.deepcopy(self.data)
        data['proposals'].append({**data['proposals'][0], 'id': 'replace-a2', 'asset': 'a2'})
        data['periods'][0]['budget'] = '3000.00'
        catalog, risk, plan = self.solve(data)
        checked = evaluate(catalog, risk, ['replace-a1', 'replace-a2'])
        self.assertFalse(checked['feasible'])
        floor = next(row for row in checked['violations'] if row['code'] == 'site_operating_floor')
        self.assertEqual(floor['remaining'], 0)
        self.assertEqual(floor['required'], 1)
        self.assertFalse({'replace-a1', 'replace-a2'} <= set(plan['selected']))
        self.assertTrue(plan['optimality_proven'])
        self.assertTrue(plan['constraint_check']['feasible'])

    def test_mandatory_infeasible_and_forced_forbidden_replay(self):
        data = copy.deepcopy(self.data)
        data['proposals'][0]['mandatory'] = True
        data['periods'][0]['budget'] = '100.00'
        catalog, risk, plan = self.solve(data)
        self.assertEqual(plan['status'], 'infeasible')
        self.assertEqual(plan['selected'], [])
        self.assertIn('budget_exceeded', [row['code'] for row in plan['violations']])
        self.assertTrue(verify(catalog, risk, plan, True)['valid'])
        catalog, risk, plan = self.solve(self.data, force=['replace-a1'], forbid=['replace-a3'])
        self.assertEqual(plan['selected'], ['replace-a1'])
        self.assertTrue(verify(catalog, risk, plan, True)['valid'])
        forbidden = optimize(Catalog({**self.data, 'proposals': [{**row, 'mandatory': row['id'] == 'replace-a1'} for row in self.data['proposals']]}), self.scored, forbid=['replace-a1'])
        self.assertEqual(forbidden['status'], 'infeasible')
        with self.assertRaises(ValueError):
            optimize(self.catalog, self.scored, force=['replace-a1'], forbid=['replace-a1'])
        with self.assertRaises(ValueError):
            optimize(self.catalog, self.scored, force=['unknown'])

    def test_search_limit_reports_feasibility_without_optimality_claim(self):
        limited = optimize(self.catalog, self.scored, maximum_nodes=1)
        self.assertEqual(limited['status'], 'feasible_search_limit')
        self.assertFalse(limited['optimality_proven'])
        self.assertEqual(limited['search']['visited_nodes'], 1)
        self.assertTrue(limited['constraint_check']['feasible'])
        self.assertTrue(verify(self.catalog, self.scored, limited, True)['valid'])
        certificate = gap(self.catalog, self.scored, limited)
        self.assertFalse(certificate['search_proved_optimal'])
        self.assertGreaterEqual(value(certificate['upper_bound']), value(limited['objective']['exposure_reduction']))
        with self.assertRaises(ValueError):
            optimize(self.catalog, self.scored, maximum_nodes=0)
        with self.assertRaises(ValueError):
            optimize(self.catalog, self.scored, mode='probability')

    def test_budget_crew_and_retirement_violations_are_explicit(self):
        data = copy.deepcopy(self.data)
        data['periods'][0]['budget'] = '1.00'
        data['periods'][0]['crew_days'] = 0
        data['assets'][0]['retired'] = '2026-11-20'
        catalog, risk, plan = self.solve(data)
        checked = evaluate(catalog, risk, ['replace-a1'])
        codes = {row['code'] for row in checked['violations']}
        self.assertTrue({'budget_exceeded', 'crew_exceeded', 'retired_before_work'} <= codes)
        self.assertNotIn('replace-a1', plan['selected'])
        data['assets'][0]['retired'] = '2026-11-12'
        catalog, risk, plan = self.solve(data)
        codes = {row['code'] for row in evaluate(catalog, risk, ['replace-a1'])['violations']}
        self.assertIn('inactive_asset', codes)
        self.assertEqual(plan['selected'], [])

    def test_exclusion_counterfactual_and_attribution(self):
        explained = exclusions(self.catalog, self.scored, self.plan)
        self.assertEqual(len(explained['rows']), 3)
        retrofit = next(row for row in explained['rows'] if row['proposal'] == 'retrofit-a1')
        self.assertFalse(retrofit['add_to_existing_feasible'])
        self.assertIn('mutually_exclusive', [row['code'] for row in retrofit['violations_if_added']])
        forced = forced_tradeoffs(self.catalog, self.scored, self.plan)
        forced_retrofit = next(row for row in forced['rows'] if row['proposal'] == 'retrofit-a1')
        self.assertTrue(forced_retrofit['feasible_when_forced'])
        self.assertIn('replace-a1', forced_retrofit['removed'])
        self.assertIn('retrofit-a1', forced_retrofit['added'])
        self.assertLessEqual(value(forced_retrofit['exposure_reduction_difference']), 0)
        attributed = objective_attribution(self.catalog, self.scored, self.plan)
        self.assertEqual(sum(value(row['exposure_reduction']) for row in attributed['rows']), value(attributed['total']))
        self.assertEqual(sum(value(row['share']) for row in attributed['rows']), 1)

    def test_budget_and_crew_frontiers_recompute_independent_scenarios(self):
        frontier = budget_frontier(self.catalog, self.scored, 'p1', ['0.00', '500.00', '1000.00', '1500.00'])
        self.assertEqual(len(frontier['rows']), 4)
        values = [value(row['exposure_reduction']) for row in frontier['rows']]
        self.assertEqual(values, sorted(values))
        self.assertEqual(frontier['rows'][0]['cost_cents'], 0)
        self.assertEqual(self.catalog.periods['p1']['budget'], '1000.00')
        crew = crew_frontier(self.catalog, self.scored, 'p1', [0, 1, 2, 4])
        self.assertEqual(crew['rows'][0]['selected'], [])
        self.assertEqual(crew['rows'][2]['selected'], ['replace-a1'])
        with self.assertRaises(ValueError):
            budget_frontier(self.catalog, self.scored, 'p1', ['500.00', '100.00'])
        with self.assertRaises(KeyError):
            crew_frontier(self.catalog, self.scored, 'unknown', [1])

    def test_uncertainty_and_weights_preserve_original_policy(self):
        before = self.catalog.fingerprint
        plans = uncertainty_portfolios(self.catalog, self.scored)
        self.assertEqual(set(plans['plans']), {'low', 'central', 'high'})
        self.assertIn('replace-a1', plans['selected_in_all_feasible_bounds'])
        shifted = weight_shift(self.catalog, self.scored, 'condition', 'age', [0, 10, 20])
        self.assertEqual(len(shifted['rows']), 3)
        self.assertEqual(shifted['rows'][1]['weights']['condition'], 30)
        self.assertEqual(shifted['rows'][1]['weights']['age'], 30)
        self.assertEqual(self.catalog.policy['weights']['condition'], 40)
        self.assertEqual(self.catalog.fingerprint, before)
        with self.assertRaises(ValueError):
            weight_shift(self.catalog, self.scored, 'condition', 'age', [41])
        with self.assertRaises(ValueError):
            weight_shift(self.catalog, self.scored, 'age', 'age', [1])

    def test_relaxed_bound_and_resource_slack_conserve(self):
        bound = relaxed_bound(self.catalog, self.scored)
        self.assertGreaterEqual(value(bound['upper_bound']), value(self.plan['objective']['exposure_reduction']))
        self.assertEqual(sum(value(row['upper_bound']) for row in bound['rows']), value(bound['upper_bound']))
        slack = resource_slack(self.catalog, self.scored, self.plan)
        self.assertEqual(slack['rows'][0]['money_used_cents'], 80000)
        self.assertEqual(slack['rows'][0]['money_unused_cents'], 20000)
        self.assertEqual(slack['rows'][0]['crew_used_days'], 2)
        self.assertEqual(slack['rows'][0]['crew_unused_days'], 8)
        self.assertEqual(value(slack['rows'][0]['money_utilization']), Fraction(4, 5))
        self.assertEqual(value(slack['rows'][0]['crew_utilization']), Fraction(1, 5))
        self.assertTrue(gap(self.catalog, self.scored, self.plan)['available'])
        with self.assertRaises(ValueError):
            relaxed_bound(self.catalog, self.scored, resource='unknown')

    def test_schedule_calendar_and_lot_money(self):
        calendar = schedule(self.catalog, self.scored, self.plan)
        self.assertTrue(calendar['all_selected_scheduled'])
        self.assertEqual(calendar['rows'][0]['start'], '2026-12-01')
        self.assertEqual(calendar['rows'][0]['end'], '2026-12-04')
        self.assertEqual(calendar['blocked'], [])
        self.assertTrue(audit(self.catalog, self.scored, self.plan, calendar)['valid'])
        inventory = daily_inventory(self.catalog, calendar)
        self.assertTrue(inventory['no_shortfalls_at_event_boundaries'])
        self.assertEqual(inventory['rows'][0]['operating'], 1)
        lots = lot_summary(self.catalog, self.plan, calendar)
        self.assertEqual(lots['total_cents'], self.plan['objective']['cost_cents'])
        self.assertEqual(lots['rows'][0]['proposals'], ['replace-a1'])
        changed = copy.deepcopy(calendar)
        changed['rows'][0]['end'] = '2026-12-05'
        with self.assertRaises(ValueError):
            audit(self.catalog, self.scored, self.plan, changed)

    def test_scenarios_are_independent_and_regret_uses_same_basis(self):
        definitions = [
            {'name': 'small', 'operations': [{'kind': 'budget', 'id': 'p1', 'value': '500.00'}]},
            {'name': 'large', 'operations': [{'kind': 'budget', 'id': 'p1', 'value': '2000.00'}]},
        ]
        evaluated = evaluate_scenarios(self.catalog, '2026-11-12', '2026-11-13', definitions)
        self.assertEqual([row['name'] for row in evaluated['rows']], ['small', 'large'])
        self.assertIn('replace-a1', evaluated['rows'][1]['selected'])
        self.assertEqual(self.catalog.periods['p1']['budget'], '1000.00')
        self.assertEqual(apply(self.catalog, []) .fingerprint, self.catalog.fingerprint)
        alternate = optimize(self.catalog, self.scored, force=['retrofit-a1'])
        compared = regret(self.catalog, self.scored, alternate, self.plan)
        self.assertTrue(compared['comparable'])
        self.assertGreaterEqual(value(compared['reference_minus_candidate']), 0)
        matrix = decision_matrix(self.catalog, self.scored, {'original': self.plan, 'alternative': alternate})
        self.assertEqual(len(matrix['rows']), 2)
        self.assertTrue(all(row['feasible_under_current_constraints'] for row in matrix['rows']))
        with self.assertRaises(ValueError):
            apply(self.catalog, [{'kind': 'budget', 'id': 'p1', 'value': '1.00'}, {'kind': 'budget', 'id': 'p1', 'value': '2.00'}])

    def test_analysis_contains_linked_risk_calendar_and_quality(self):
        result = analyze(self.catalog, '2026-11-12', '2026-11-13')
        self.assertEqual(result['risk']['score_digest'], result['plan']['score_digest'])
        self.assertEqual(result['candidate_calendar']['plan_digest'], result['plan']['plan_digest'])
        self.assertEqual(result['quality']['source_digest'], self.catalog.fingerprint)
        self.assertEqual(result['procurement_lots']['total_cents'], result['plan']['objective']['cost_cents'])
        revised = apply(self.catalog, [{'kind': 'budget', 'id': 'p1', 'value': '500.00'}])
        after = analyze(revised, '2026-11-12', '2026-11-13')
        changes = compare(result, after)
        self.assertIn('replace-a1', changes['removed_proposals'])
        self.assertNotEqual(result['analysis_digest'], after['analysis_digest'])
        self.assertEqual(changes['risk_changes'], [])
        self.assertEqual(result['plan']['selected'], ['replace-a1'])


if __name__ == '__main__':
    unittest.main()
