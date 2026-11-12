import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from equipment_domain.catalog import Catalog
from equipment_domain.values import canonical, bounded_file
from decisions.analysis import verify
from decisions.query import csv_report
from risk_model.portfolio import public_summary


def contents(catalog, result, minimum_sites=3):
    checked = verify(catalog, result, True)
    files = {
        'source.json': canonical(catalog.document),
        'analysis.json': canonical(result),
        'audit.json': canonical(checked),
        'public.json': canonical(public_summary(result['risk'], minimum_sites=minimum_sites)),
        'risks.csv': csv_report(catalog, result, 'risks'),
        'selected.csv': csv_report(catalog, result, 'selected'),
    }
    if result['plan']['status'] != 'infeasible':
        files['periods.csv'] = csv_report(catalog, result, 'periods')
    manifest = {
        'version': 1,
        'input_digest': catalog.fingerprint,
        'analysis_digest': result['analysis_digest'],
        'minimum_sites': minimum_sites,
        'files': {name: hashlib.sha256(body.encode()).hexdigest() for name, body in sorted(files.items())},
        'access': 'restricted_asset_condition_and_financial_proposals',
    }
    files['manifest.json'] = canonical(manifest)
    return files


def export(catalog, result, directory, minimum_sites=3):
    files = contents(catalog, result, minimum_sites)
    target = Path(directory)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.renewal-', dir=target.parent))
    try:
        for name, body in files.items():
            with (stage / name).open('w', encoding='utf-8', newline='') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
        os.rename(stage, target)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return inspect(target)


def inspect(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('bundle must be regular directory')
    manifest = json.loads(bounded_file(root / 'manifest.json', 100000).read_text())
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('invalid bundle manifest')
    if {path.name for path in root.iterdir()} != set(manifest['files']) | {'manifest.json'}:
        raise ValueError('bundle file set mismatch')
    for name, checksum in manifest['files'].items():
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe manifest file name')
        path = bounded_file(root / name, 100_000_000)
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('bundle checksum mismatch')
    catalog = Catalog(json.loads(bounded_file(root / 'source.json').read_text()))
    result = json.loads(bounded_file(root / 'analysis.json', 100_000_000).read_text())
    expected = contents(catalog, result, manifest['minimum_sites'])
    for name, body in expected.items():
        if bounded_file(root / name, 100_000_000).read_bytes() != body.encode():
            raise ValueError('bundle semantic replay mismatch')
    return {'verified': True, 'files': len(expected), 'analysis_digest': result['analysis_digest']}
