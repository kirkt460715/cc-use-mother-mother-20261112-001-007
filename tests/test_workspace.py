import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from equipment_domain.catalog import Catalog
from equipment_domain.values import digest, canonical
from equipment_domain.exchange import TABLES, read_table, write_table, export_directory, import_directory, patch_catalog
from equipment_domain.change_impact import compare, site_impact, patch_plan
from decisions.analysis import analyze, verify
from decisions.workspace import Workspace, Conflict
from decisions import recovery, package, query, monitor
import renewal


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = json.loads(Path('fixtures/equipment.json').read_text())
        self.catalog = Catalog(self.data)
        self.workspace = Workspace(self.root / 'db.sqlite')
        self.workspace.create('source', self.data)
        self.run = self.workspace.run('run', 'source', 1, '2026-11-12', '2026-11-13')

    def tearDown(self):
        self.workspace.close()
        self.temp.cleanup()

    def approved(self):
        self.workspace.propose('run', 'planner')
        return self.workspace.decide('run', 1, 'reviewer', True, 'Evidence reviewed')

    def test_create_idempotency_and_document_ownership(self):
        result = self.workspace.create('source', self.data)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['input_digest'], self.catalog.fingerprint)
        result['document']['sites'][0]['users'] = 12
        self.assertEqual(self.workspace.source('source')['document']['sites'][0]['users'], 100)
        altered = copy.deepcopy(self.data)
        altered['sites'][0]['users'] = 110
        with self.assertRaises(Conflict):
            self.workspace.create('source', altered)
        with self.assertRaises(KeyError):
            self.workspace.source('absent')
        self.assertEqual(self.workspace.audit()['sources'], 1)

    def test_revise_requires_compare_and_swap(self):
        changed = copy.deepcopy(self.data)
        changed['sites'][0]['users'] = 80
        same = self.workspace.revise('source', 1, self.data)
        self.assertEqual(same['revision'], 1)
        result = self.workspace.revise('source', 1, changed)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['document']['sites'][0]['users'], 80)
        with self.assertRaises(Conflict):
            self.workspace.revise('source', 1, self.data)
        with self.assertRaises(Conflict):
            self.workspace.run('stale', 'source', 1, '2026-11-12', '2026-11-13')
        self.assertEqual(self.workspace.result('run'), self.run)
        self.assertEqual(self.workspace.catalog('run').fingerprint, self.catalog.fingerprint)

    def test_independent_connections_observe_revision_conflict(self):
        with Workspace(self.root / 'db.sqlite') as second:
            read = second.source('source')
            changed = copy.deepcopy(self.data)
            changed['sites'][0]['users'] = 99
            self.workspace.revise('source', 1, changed)
            with self.assertRaises(Conflict):
                second.revise('source', read['revision'], self.data)
            self.assertEqual(second.source('source')['revision'], 2)
            self.assertEqual(second.result('run')['result'], self.run['result'])
        self.assertEqual(self.workspace.audit()['runs'], 1)

    def test_nested_transaction_rolls_back_only_savepoint(self):
        with self.workspace.atomic():
            self.workspace.create('outer', self.data)
            with self.assertRaises(RuntimeError):
                with self.workspace.atomic():
                    self.workspace.create('inner', self.data)
                    raise RuntimeError('simulated failure')
            self.assertEqual(self.workspace.source('outer')['revision'], 1)
            with self.assertRaises(KeyError):
                self.workspace.source('inner')
        self.assertEqual(self.workspace.audit()['sources'], 2)
        with self.assertRaises(RuntimeError):
            with self.workspace.atomic():
                self.workspace.create('rolled-back', self.data)
                raise RuntimeError('abort whole write')
        with self.assertRaises(KeyError):
            self.workspace.source('rolled-back')

    def test_branch_uses_expected_head_and_is_independent(self):
        branch = self.workspace.branch('source', 1, 'alternative')
        self.assertEqual(branch['input_digest'], self.catalog.fingerprint)
        changed = branch['document']
        changed['sites'][0]['users'] = 19
        revised = self.workspace.revise('alternative', 1, changed)
        self.assertEqual(revised['revision'], 2)
        self.assertEqual(self.workspace.source('source')['revision'], 1)
        with self.assertRaises(Conflict):
            self.workspace.branch('alternative', 1, 'third')
        with self.assertRaises(Conflict):
            self.workspace.branch('source', 1, 'source')
        self.assertEqual(self.workspace.audit()['sources'], 2)

    def test_run_identity_and_snapshot_binding(self):
        same = self.workspace.run('run', 'source', 1, '2026-11-12', '2026-11-13')
        self.assertEqual(same, self.run)
        with self.assertRaises(Conflict):
            self.workspace.run('run', 'source', 1, '2026-11-13', '2026-11-13')
        with self.assertRaises(Conflict):
            self.workspace.run('run', 'source', 1, '2026-11-12', '2026-11-13', 'high')
        self.assertEqual(verify(self.catalog, self.run['result'], True)['valid'], True)
        with self.assertRaises(KeyError):
            self.workspace.result('absent')
        self.assertEqual(self.workspace.audit()['runs'], 1)

    def test_review_separates_proposal_from_approval(self):
        first = self.workspace.propose('run', 'planner')
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(first['revision'], 1)
        self.assertEqual(self.workspace.propose('run', 'planner'), first)
        with self.assertRaises(Conflict):
            self.workspace.propose('run', 'other')
        with self.assertRaises(Conflict):
            self.workspace.decide('run', 1, 'planner', True, 'self')
        with self.assertRaises(Conflict):
            self.workspace.decide('run', 2, 'reviewer', True, 'stale')
        with self.assertRaises(ValueError):
            self.workspace.decide('run', 1, 'reviewer', 1, 'invalid boolean')
        self.assertEqual(self.workspace.review('run')['state'], 'pending')

    def test_approved_publication_is_immutable(self):
        approved = self.approved()
        self.assertEqual(approved['revision'], 2)
        self.assertEqual(approved['state'], 'approved')
        self.assertEqual(len(approved['history']), 2)
        publication = self.workspace.publish('budget-v1', 'run')
        self.assertEqual(publication['digest'], self.run['result']['analysis_digest'])
        self.assertEqual(self.workspace.publish('budget-v1', 'run'), publication)
        with self.assertRaises(Conflict):
            self.workspace.decide('run', 2, 'other', False, 'later rejection')
        self.assertEqual(self.workspace.audit()['labels'], 1)
        self.assertEqual(self.workspace.audit()['reviews'], 1)

    def test_pending_and_rejected_cannot_be_published(self):
        self.workspace.propose('run', 'planner')
        with self.assertRaises(Conflict):
            self.workspace.publish('pending', 'run')
        result = self.workspace.decide('run', 1, 'reviewer', False, 'missing inspection')
        self.assertEqual(result['state'], 'rejected')
        self.assertEqual(result['reason'], 'missing inspection')
        with self.assertRaises(Conflict):
            self.workspace.publish('rejected', 'run')
        self.assertEqual(self.workspace.audit()['labels'], 0)
        self.assertEqual(self.workspace.pending_reviews()['rows'], [])

    def test_pending_queue_paginates_and_excludes_completed(self):
        self.workspace.propose('run', 'planner')
        self.workspace.run('second', 'source', 1, '2026-11-12', '2026-11-13')
        self.workspace.propose('second', 'planner')
        first = self.workspace.pending_reviews(limit=1)
        self.assertEqual([r['run'] for r in first['rows']], ['run'])
        self.assertEqual(first['next_after'], 'run')
        second = self.workspace.pending_reviews(limit=1, after=first['next_after'])
        self.assertEqual([r['run'] for r in second['rows']], ['second'])
        self.assertIsNone(second['next_after'])
        self.workspace.decide('run', 1, 'reviewer', True, 'ok')
        self.assertEqual([r['run'] for r in self.workspace.pending_reviews()['rows']], ['second'])
        with self.assertRaises(ValueError):
            self.workspace.pending_reviews(0)

    def test_review_history_corruption_is_detected(self):
        self.approved()
        self.workspace.db.execute("UPDATE review_events SET previous='forged' WHERE revision=2")
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.workspace.review('run')
        with self.assertRaises(ValueError):
            self.workspace.audit()
        self.assertEqual(self.workspace.db.execute('SELECT COUNT(*) FROM labels').fetchone()[0], 0)
        self.assertEqual(self.workspace.db.execute('SELECT COUNT(*) FROM review_events').fetchone()[0], 2)

    def test_run_corruption_does_not_silently_repair(self):
        self.workspace.db.execute("UPDATE runs SET digest='wrong' WHERE id='run'")
        with self.assertRaisesRegex(ValueError, 'binding'):
            self.workspace.result('run')
        with self.assertRaises(ValueError):
            self.workspace.audit()
        stored = self.workspace.db.execute("SELECT digest FROM runs WHERE id='run'").fetchone()[0]
        self.assertEqual(stored, 'wrong')
        self.assertEqual(self.workspace.source('source')['revision'], 1)

    def test_backup_restore_preserves_approval_and_labels(self):
        self.approved()
        self.workspace.publish('approved', 'run')
        saved = recovery.backup(self.workspace, self.root / 'backup.db')
        self.assertEqual(saved['audit']['labels'], 1)
        checksum = hashlib.sha256((self.root / 'backup.db').read_bytes()).hexdigest()
        self.assertEqual(saved['sha256'], checksum)
        restored = recovery.restore(self.root / 'backup.db', self.root / 'restored.db', checksum)
        self.assertEqual(restored['audit'], saved['audit'])
        with Workspace(self.root / 'restored.db') as db:
            self.assertEqual(db.result('run'), self.run)
            self.assertEqual(db.publication('approved')['run'], 'run')
        self.assertTrue(restored['restored'])

    def test_restore_refuses_wrong_checksum_and_existing_target(self):
        saved = recovery.backup(self.workspace, self.root / 'backup.db')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            recovery.restore(self.root / 'backup.db', self.root / 'bad.db', '0' * 64)
        self.assertFalse((self.root / 'bad.db').exists())
        with self.assertRaises(FileExistsError):
            recovery.backup(self.workspace, self.root / 'backup.db')
        with self.assertRaises(FileExistsError):
            recovery.restore(self.root / 'backup.db', self.root / 'db.sqlite', saved['sha256'])
        self.assertEqual(self.workspace.result('run'), self.run)
        self.assertEqual(self.workspace.audit()['runs'], 1)

    def test_rebuild_uses_frozen_source_and_drops_decisions(self):
        self.approved()
        self.workspace.publish('approved', 'run')
        changed = copy.deepcopy(self.data)
        changed['sites'][0]['users'] = 150
        self.workspace.revise('source', 1, changed)
        rebuilt = recovery.rebuild_runs(self.workspace, self.root / 'rebuilt.db')
        self.assertEqual(rebuilt['replayed_runs'], 1)
        self.assertTrue(rebuilt['reviews_and_labels_not_copied'])
        self.assertEqual(rebuilt['audit']['labels'], 0)
        with Workspace(self.root / 'rebuilt.db') as db:
            self.assertEqual(db.result('run')['result'], self.run['result'])
            self.assertEqual(db.catalog('run').fingerprint, self.catalog.fingerprint)
            self.assertEqual(db.audit()['reviews'], 0)

    def test_bundle_replays_restricted_and_public_outputs(self):
        exported = package.export(self.catalog, self.run['result'], self.root / 'bundle')
        self.assertTrue(exported['verified'])
        self.assertEqual(package.inspect(self.root / 'bundle'), exported)
        self.assertEqual(exported['analysis_digest'], self.run['result']['analysis_digest'])
        manifest = json.loads((self.root / 'bundle/manifest.json').read_text())
        self.assertEqual(manifest['input_digest'], self.catalog.fingerprint)
        self.assertIn('restricted', manifest['access'])
        with self.assertRaises(FileExistsError):
            package.export(self.catalog, self.run['result'], self.root / 'bundle')
        self.assertTrue((self.root / 'bundle/selected.csv').exists())

    def test_bundle_tamper_and_extra_file_rejected(self):
        package.export(self.catalog, self.run['result'], self.root / 'bundle')
        extra = self.root / 'bundle/extra.txt'
        extra.write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'file set'):
            package.inspect(self.root / 'bundle')
        extra.unlink()
        target = self.root / 'bundle/selected.csv'
        target.write_text(target.read_text() + 'tampered\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            package.inspect(self.root / 'bundle')
        self.assertEqual(self.workspace.result('run'), self.run)
        self.assertFalse((self.root / 'bundle/extra.txt').exists())

    def test_csv_tables_roundtrip_typed_values(self):
        for table in TABLES:
            with self.subTest(table=table):
                text = write_table(self.catalog, table)
                rows = read_table(table, text)
                self.assertEqual(rows, self.catalog.document[table])
                self.assertTrue(text.endswith('\n'))
        exported = export_directory(self.catalog, self.root / 'csv')
        self.assertEqual(exported['input_digest'], self.catalog.fingerprint)
        self.assertEqual(len(exported['files']), len(TABLES) + 1)
        self.assertEqual(import_directory(self.root / 'csv').fingerprint, self.catalog.fingerprint)
        self.assertEqual(self.catalog.document, self.data)

    def test_csv_bad_header_width_boolean_and_extra_files(self):
        with self.assertRaises(ValueError):
            read_table('sites', 'id,id\na,b\n')
        csv = write_table(self.catalog, 'sites')
        with self.assertRaises(ValueError):
            read_table('sites', csv.replace('false', 'False'))
        with self.assertRaises(ValueError):
            read_table('sites', csv + 'incomplete\n')
        export_directory(self.catalog, self.root / 'csv')
        (self.root / 'csv/unknown.csv').write_text('id\n')
        with self.assertRaises(ValueError):
            import_directory(self.root / 'csv')
        self.assertEqual(self.workspace.source('source')['revision'], 1)

    def test_patch_is_atomic_and_optimistic(self):
        original = self.catalog.sites['s1']
        after = {**original, 'users': 75}
        patch = {'table': 'sites', 'id': 's1', 'before_digest': digest(original), 'after': after}
        changed = patch_catalog(self.catalog, [patch])
        self.assertEqual(changed.sites['s1']['users'], 75)
        self.assertEqual(self.catalog.sites['s1']['users'], 100)
        with self.assertRaisesRegex(ValueError, 'optimistic'):
            patch_catalog(changed, [patch])
        with self.assertRaises(ValueError):
            patch_catalog(self.catalog, [patch, patch])
        self.assertEqual(self.catalog.fingerprint, digest(self.data))
        self.assertNotEqual(changed.fingerprint, self.catalog.fingerprint)

    def test_patch_reference_validation_and_target_identity(self):
        row = self.catalog.sites['s1']
        remove = {'table': 'sites', 'id': 's1', 'before_digest': digest(row), 'after': None}
        with self.assertRaises(ValueError):
            patch_catalog(self.catalog, [remove])
        wrong = {**remove, 'after': {**row, 'id': 'other'}}
        with self.assertRaisesRegex(ValueError, 'preserve'):
            patch_catalog(self.catalog, [wrong])
        absent = {'table': 'sites', 'id': 'absent', 'before_digest': None, 'after': None}
        with self.assertRaises(ValueError):
            patch_catalog(self.catalog, [absent])
        self.assertIn('s1', self.catalog.sites)
        self.assertEqual(self.catalog.fingerprint, digest(self.data))

    def test_change_impact_tracks_site_and_budget_dependencies(self):
        changed = copy.deepcopy(self.data)
        changed['sites'][0]['users'] += 1
        other = Catalog(changed)
        difference = compare(self.catalog, other)
        self.assertTrue(difference['optimization_must_recompute_all_candidates'])
        self.assertIn('a1', difference['potentially_affected_assets'])
        self.assertEqual(difference['changes'][0]['fields'], ['users'])
        self.assertFalse(difference['policy_changed'])
        impacted = site_impact(self.catalog, other)
        self.assertIn('s1', [r['site'] for r in impacted['rows']])
        plan = patch_plan(self.catalog, other)
        replayed = patch_catalog(self.catalog, plan['operations'])
        self.assertEqual(replayed.fingerprint, other.fingerprint)
        self.assertIsNone(plan['policy_replacement'])

    def test_query_cursor_binds_projection_and_analysis(self):
        result = self.run['result']
        first = query.assets(result, columns=['asset'], limit=1)
        self.assertEqual(len(first['rows']), 1)
        self.assertIsNotNone(first['next_cursor'])
        second = query.assets(result, columns=['asset'], limit=1, cursor=first['next_cursor'])
        self.assertNotEqual(first['rows'][0]['asset'], second['rows'][0]['asset'])
        self.assertEqual(first['total'], len(result['risk']['rows']))
        with self.assertRaisesRegex(ValueError, 'cursor'):
            query.assets(result, columns=['asset', 'band'], cursor=first['next_cursor'])
        with self.assertRaises(ValueError):
            query.assets(result, cursor='not+base64?')
        self.assertEqual(query.assets(result, filters={'site': ['absent']})['rows'], [])

    def test_query_invalid_filters_and_projection(self):
        result = self.run['result']
        for options in ({'filters': {'unknown': []}}, {'filters': {'active': [1]}},
                        {'columns': []}, {'columns': ['asset', 'asset']}, {'limit': 0}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    query.assets(result, **options)
        detail = query.asset_detail(self.catalog, result, 'a1')
        self.assertEqual(detail['asset']['id'], 'a1')
        self.assertEqual(detail['risk']['asset'], 'a1')
        with self.assertRaises(KeyError):
            query.asset_detail(self.catalog, result, 'absent')
        self.assertEqual(result, self.workspace.result('run')['result'])

    def test_report_dispatch_and_csv_keep_exact_money(self):
        result = self.run['result']
        for kind in ('risk', 'plan', 'calendar', 'quality', 'lots', 'inventory', 'overlaps'):
            self.assertIsNotNone(query.report(self.catalog, result, kind))
        for kind in ('risks', 'selected', 'periods'):
            text = query.csv_report(self.catalog, result, kind)
            self.assertTrue(text.endswith('\n'))
            self.assertGreater(len(text.splitlines()), 1)
        with self.assertRaises(ValueError):
            query.report(self.catalog, result, 'unknown')
        with self.assertRaises(ValueError):
            query.report(self.catalog, result, 'plan', {'unknown': 1})
        with self.assertRaises(ValueError):
            query.csv_report(self.catalog, result, 'unknown')
        self.assertEqual(self.workspace.audit()['runs'], 1)

    def test_monitor_keeps_prior_approval_frozen(self):
        self.approved()
        before = self.workspace.result('run')
        changed = copy.deepcopy(self.data)
        changed['sites'][0]['users'] += 10
        self.workspace.revise('source', 1, changed)
        result = monitor.reassess(self.workspace, 'run', 'source', '2026-11-12', '2026-11-13')
        self.assertEqual(result['current_revision'], 2)
        self.assertTrue(result['original_approval_not_extended_to_current_terms'])
        self.assertFalse(result['original_snapshot_modified'])
        self.assertEqual(monitor.verify(result)['valid'], True)
        self.assertEqual(self.workspace.result('run'), before)
        self.assertEqual(self.workspace.review('run')['state'], 'approved')
        self.assertTrue(result['source_changes']['optimization_must_recompute_all_candidates'])

    def test_monitor_rejects_time_reversal_and_digest_tampering(self):
        with self.assertRaises(ValueError):
            monitor.reassess(self.workspace, 'run', 'source', '2026-11-11', '2026-11-13')
        with self.assertRaises(ValueError):
            monitor.reassess(self.workspace, 'run', 'source', '2026-11-12', '2026-11-12')
        result = monitor.reassess(self.workspace, 'run', 'source', '2026-11-12', '2026-11-13')
        self.assertTrue(monitor.verify(result)['valid'])
        result['current_revision'] = 8
        with self.assertRaises(ValueError):
            monitor.verify(result)
        self.assertEqual(self.workspace.source('source')['revision'], 1)
        self.assertEqual(self.workspace.audit()['runs'], 1)

    def test_cli_returns_structured_error_and_success(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = renewal.main(['analyze', '--input', str(self.root / 'missing'),
                                 '--as-of', '2026-11-12', '--known-at', '2026-11-13'])
        self.assertEqual(code, 2)
        self.assertIn('error', json.loads(stderr.getvalue()))
        self.assertEqual(stdout.getvalue(), '')
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = renewal.main(['report', '--database', str(self.root / 'db.sqlite'),
                                 '--id', 'run', '--kind', 'plan'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), self.run['result']['plan'])
        self.assertEqual(self.workspace.audit()['labels'], 0)

    def test_csv_directory_rejects_symlink_and_missing_table(self):
        export_directory(self.catalog, self.root / 'tables')
        target = self.root / 'tables/sites.csv'
        saved = target.read_text()
        target.unlink()
        with self.assertRaises(ValueError):
            import_directory(self.root / 'tables')
        outside = self.root / 'sites.csv'
        outside.write_text(saved)
        target.symlink_to(outside)
        with self.assertRaises(ValueError):
            import_directory(self.root / 'tables')
        target.unlink()
        target.write_text(saved)
        self.assertEqual(import_directory(self.root / 'tables').fingerprint, self.catalog.fingerprint)
        self.assertEqual(outside.read_text(), saved)

    def test_publication_label_cannot_point_to_a_different_run(self):
        self.approved()
        original = self.workspace.publish('fixed', 'run')
        self.workspace.run('second', 'source', 1, '2026-11-12', '2026-11-13')
        self.workspace.propose('second', 'planner')
        self.workspace.decide('second', 1, 'reviewer', True, 'same terms separate run')
        with self.assertRaises(Conflict):
            self.workspace.publish('fixed', 'second')
        self.assertEqual(self.workspace.publication('fixed'), original)
        self.assertEqual(self.workspace.audit()['labels'], 1)
        self.assertEqual(self.workspace.audit()['runs'], 2)
        self.assertEqual(self.workspace.audit()['reviews'], 2)

    def test_source_corruption_detected_before_new_analysis(self):
        self.workspace.db.execute("UPDATE sources SET digest='bad' WHERE id='source'")
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.workspace.source('source')
        with self.assertRaises(ValueError):
            self.workspace.run('new', 'source', 1, '2026-11-12', '2026-11-13')
        self.assertEqual(self.workspace.result('run'), self.run)
        self.assertEqual(self.workspace.catalog('run').fingerprint, self.catalog.fingerprint)
        count = self.workspace.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(ValueError):
            self.workspace.audit()
        self.assertEqual(self.workspace.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
