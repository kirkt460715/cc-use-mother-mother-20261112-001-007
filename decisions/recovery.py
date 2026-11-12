import hashlib
from pathlib import Path
import sqlite3
from decisions.workspace import Workspace
from equipment_domain.values import bounded_file


def backup(workspace, destination):
    evidence = workspace.audit()
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    try:
        connection = sqlite3.connect(str(target))
        try:
            workspace.db.backup(connection)
        finally:
            connection.close()
        with Workspace(target) as restored:
            if restored.audit() != evidence:
                raise ValueError('backup semantic audit mismatch')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'audit': evidence}


def restore(source, destination, checksum):
    origin = bounded_file(source, 500_000_000)
    if hashlib.sha256(origin.read_bytes()).hexdigest() != checksum:
        raise ValueError('backup checksum mismatch')
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    try:
        source_db = sqlite3.connect(origin.resolve().as_uri() + '?mode=ro', uri=True)
        target_db = sqlite3.connect(str(target))
        try:
            source_db.backup(target_db)
        finally:
            source_db.close()
            target_db.close()
        with Workspace(target) as workspace:
            evidence = workspace.audit()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'restored': True, 'audit': evidence}


def rebuild_runs(workspace, destination):
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    count = 0
    try:
        with Workspace(target) as rebuilt:
            for row in workspace.db.execute('SELECT id FROM runs ORDER BY id'):
                key = row[0]
                original = workspace.result(key)
                source = workspace.catalog(key)
                request = original['request']
                source_id = 'snapshot-' + key
                rebuilt.create(source_id, source.document)
                actual = rebuilt.run(key, source_id, 1, request['as_of'], request['known_at'], request['mode'], request['maximum_nodes'])
                if actual['result'] != original['result']:
                    raise ValueError('rebuilt analysis differs from frozen run')
                count += 1
            evidence = rebuilt.audit()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'replayed_runs': count, 'audit': evidence, 'reviews_and_labels_not_copied': True, 'each_run_uses_its_frozen_source_snapshot': True}
