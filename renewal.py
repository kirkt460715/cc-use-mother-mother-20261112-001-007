import argparse
from pathlib import Path
import sys
import tempfile
from equipment_domain.catalog import Catalog
from equipment_domain.values import load, canonical, bounded_file
from equipment_domain.exchange import export_directory, import_directory, patch_catalog
from decisions.analysis import analyze, compare, verify
from decisions.workspace import Workspace
from decisions.query import report, csv_report
from decisions import package, recovery


def demo(fixture):
    catalog = Catalog(load(fixture))
    with tempfile.TemporaryDirectory(prefix='renewal-demo-') as directory:
        root = Path(directory)
        with Workspace(root / 'renewal.db') as workspace:
            source = workspace.create('catalog', catalog.document)
            first = workspace.run('initial', 'catalog', source['revision'], '2026-11-12', '2026-11-13')
            workspace.propose('initial', 'planner')
            workspace.decide('initial', 1, 'reviewer', True, 'reviewed synthetic decision')
            published = workspace.publish('approved-v1', 'initial')
            exported = package.export(catalog, first['result'], root / 'bundle')
            exchange = export_directory(catalog, root / 'tables')
            if import_directory(root / 'tables').fingerprint != catalog.fingerprint:
                raise ValueError('CSV roundtrip failed')
            saved = recovery.backup(workspace, root / 'backup.db')
            restored = recovery.restore(root / 'backup.db', root / 'restored.db', saved['sha256'])
            rebuilt = recovery.rebuild_runs(workspace, root / 'rebuilt.db')
            return {
                'plan_status': first['result']['plan']['status'],
                'selected': first['result']['plan']['selected'],
                'objective': first['result']['plan']['objective'],
                'publication': published,
                'bundle': exported,
                'csv': exchange,
                'restore': restored,
                'replay': rebuilt,
                'source': 'local_synthetic_fixture',
                'runtime_downloads': False,
            }


def parser():
    root = argparse.ArgumentParser(description='Equipment renewal risk and constrained investment decisions')
    commands = root.add_subparsers(dest='command', required=True)
    monitor = commands.add_parser('monitor')
    monitor.add_argument('--database', required=True)
    monitor.add_argument('--id', required=True)
    monitor.add_argument('--source', required=True)
    monitor.add_argument('--as-of', required=True)
    monitor.add_argument('--known-at', required=True)
    diff = commands.add_parser('compare-source')
    diff.add_argument('--before', required=True)
    diff.add_argument('--after', required=True)
    demonstration = commands.add_parser('demo')
    demonstration.add_argument('--fixture', required=True)
    direct = commands.add_parser('analyze')
    direct.add_argument('--input', required=True)
    run = commands.add_parser('run')
    run.add_argument('--database', required=True)
    run.add_argument('--id', required=True)
    run.add_argument('--source', required=True)
    run.add_argument('--revision', type=int, required=True)
    for command in (direct, run):
        command.add_argument('--as-of', required=True)
        command.add_argument('--known-at', required=True)
        command.add_argument('--mode', choices=['low', 'central', 'high'], default='central')
        command.add_argument('--maximum-nodes', type=int, default=500000)
    for name in ('create', 'revise', 'patch'):
        command = commands.add_parser(name)
        command.add_argument('--database', required=True)
        command.add_argument('--source', required=True)
        command.add_argument('--input', required=True)
        if name != 'create':
            command.add_argument('--revision', type=int, required=True)
    branch = commands.add_parser('branch')
    branch.add_argument('--database', required=True)
    branch.add_argument('--source', required=True)
    branch.add_argument('--revision', type=int, required=True)
    branch.add_argument('--target', required=True)
    query = commands.add_parser('report')
    query.add_argument('--database', required=True)
    query.add_argument('--id', required=True)
    query.add_argument('--kind', required=True)
    query.add_argument('--options')
    query.add_argument('--csv', action='store_true')
    proposal = commands.add_parser('propose')
    proposal.add_argument('--database', required=True)
    proposal.add_argument('--id', required=True)
    proposal.add_argument('--actor', required=True)
    decision = commands.add_parser('decide')
    decision.add_argument('--database', required=True)
    decision.add_argument('--id', required=True)
    decision.add_argument('--actor', required=True)
    decision.add_argument('--revision', type=int, required=True)
    decision.add_argument('--reason', required=True)
    decision.add_argument('--approve', action='store_true')
    publication = commands.add_parser('publish')
    publication.add_argument('--database', required=True)
    publication.add_argument('--id', required=True)
    publication.add_argument('--label', required=True)
    export = commands.add_parser('export')
    export.add_argument('--database', required=True)
    export.add_argument('--id', required=True)
    export.add_argument('--directory', required=True)
    inspect = commands.add_parser('verify-bundle')
    inspect.add_argument('--directory', required=True)
    for name in ('csv-in', 'csv-out'):
        command = commands.add_parser(name)
        command.add_argument('--input', required=True)
        command.add_argument('--output', required=True)
    for name in ('audit', 'backup', 'rebuild'):
        command = commands.add_parser(name)
        command.add_argument('--database', required=True)
        if name != 'audit':
            command.add_argument('--output', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--input', required=True)
    restore.add_argument('--output', required=True)
    restore.add_argument('--sha256', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--database', required=True)
    serve.add_argument('--token-file', required=True)
    serve.add_argument('--port', type=int, default=8080)
    return root


def execute(args):
    command = args.command
    if command == 'compare-source':
        from equipment_domain.change_impact import compare, site_impact, patch_plan
        first, last = Catalog(load(args.before)), Catalog(load(args.after))
        return {'difference': compare(first, last), 'sites': site_impact(first, last), 'patch': patch_plan(first, last)}
    if command == 'demo':
        return demo(args.fixture)
    if command == 'analyze':
        catalog = Catalog(load(args.input))
        result = analyze(catalog, args.as_of, args.known_at, args.mode, args.maximum_nodes)
        verify(catalog, result)
        return result
    if command == 'csv-out':
        return export_directory(Catalog(load(args.input)), args.output)
    if command == 'csv-in':
        catalog = import_directory(args.input)
        with Path(args.output).open('x', encoding='utf-8') as output:
            output.write(canonical(catalog.document))
        return {'input_digest': catalog.fingerprint}
    if command == 'verify-bundle':
        return package.inspect(args.directory)
    if command == 'restore':
        return recovery.restore(args.input, args.output, args.sha256)
    if command == 'serve':
        from decisions.http_api import serve
        path = bounded_file(args.token_file, 10000)
        if path.stat().st_mode & 0o077:
            raise ValueError('token file must be owner-only')
        serve(args.database, path.read_text().strip(), args.port)
        return {'stopped': True}
    with Workspace(args.database) as workspace:
        if command == 'monitor':
            from decisions.monitor import reassess, verify as verify_monitor
            result = reassess(workspace, args.id, args.source, args.as_of, args.known_at)
            verify_monitor(result)
            return result
        if command == 'create':
            return workspace.create(args.source, load(args.input))
        if command == 'revise':
            return workspace.revise(args.source, args.revision, load(args.input))
        if command == 'patch':
            current = workspace.source(args.source)
            if current['revision'] != args.revision:
                from decisions.workspace import Conflict
                raise Conflict('patch source revision changed')
            catalog = patch_catalog(Catalog(current['document']), load(args.input))
            return workspace.revise(args.source, args.revision, catalog.document)
        if command == 'branch':
            return workspace.branch(args.source, args.revision, args.target)
        if command == 'run':
            return workspace.run(args.id, args.source, args.revision, args.as_of, args.known_at, args.mode, args.maximum_nodes)
        if command == 'report':
            catalog = workspace.catalog(args.id)
            result = workspace.result(args.id)['result']
            if args.csv:
                return csv_report(catalog, result, args.kind)
            return report(catalog, result, args.kind, load(args.options) if args.options else None)
        if command == 'propose':
            return workspace.propose(args.id, args.actor)
        if command == 'decide':
            return workspace.decide(args.id, args.revision, args.actor, args.approve, args.reason)
        if command == 'publish':
            return workspace.publish(args.label, args.id)
        if command == 'export':
            return package.export(workspace.catalog(args.id), workspace.result(args.id)['result'], args.directory)
        if command == 'audit':
            return workspace.audit()
        if command == 'backup':
            return recovery.backup(workspace, args.output)
        if command == 'rebuild':
            return recovery.rebuild_runs(workspace, args.output)
    raise ValueError('unsupported command')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (ValueError, KeyError, OSError) as exc:
        print(canonical({'error': str(exc)}), file=sys.stderr)
        return 2
    print(result if isinstance(result, str) else canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
