from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from equipment_domain.catalog import Catalog
from equipment_domain.values import canonical, digest, ident, text, integer, fields
from decisions.analysis import analyze, verify


class Conflict(ValueError):
    pass


class Workspace:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS sources(
            id TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            revision INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL REFERENCES sources(id),
            revision INTEGER NOT NULL,
            request TEXT NOT NULL,
            source_body TEXT NOT NULL,
            result TEXT NOT NULL,
            digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews(
            run TEXT PRIMARY KEY REFERENCES runs(id),
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            proposer TEXT NOT NULL,
            reviewer TEXT,
            reason TEXT,
            run_digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_events(
            run TEXT NOT NULL REFERENCES reviews(run),
            revision INTEGER NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            previous TEXT,
            PRIMARY KEY(run,revision)
        );
        CREATE TABLE IF NOT EXISTS labels(
            name TEXT PRIMARY KEY,
            run TEXT NOT NULL REFERENCES runs(id),
            digest TEXT NOT NULL
        );
        ''')
        self.serial = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

    @contextmanager
    def atomic(self):
        nested = self.db.in_transaction
        self.serial += 1
        point = 'workspace_' + str(self.serial)
        self.db.execute('SAVEPOINT ' + point if nested else 'BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            if nested:
                self.db.execute('ROLLBACK TO ' + point)
                self.db.execute('RELEASE ' + point)
            else:
                self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('RELEASE ' + point if nested else 'COMMIT')

    def create(self, key, document):
        ident(key, 'source.id')
        catalog = Catalog(document)
        with self.atomic():
            row = self.db.execute('SELECT * FROM sources WHERE id=?', (key,)).fetchone()
            if row and row['digest'] != catalog.fingerprint:
                raise Conflict('source identifier already exists with different content')
            self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?,1)', (key, canonical(catalog.document), catalog.fingerprint))
        return self.source(key)

    def source(self, key):
        row = self.db.execute('SELECT * FROM sources WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        catalog = Catalog(json.loads(row['body']))
        if catalog.fingerprint != row['digest']:
            raise ValueError('workspace source digest mismatch')
        return {'id': key, 'revision': row['revision'], 'input_digest': catalog.fingerprint, 'document': catalog.document}

    def revise(self, key, revision, document):
        integer(revision, 'revision', 1)
        catalog = Catalog(document)
        with self.atomic():
            current = self.source(key)
            if current['revision'] != revision:
                raise Conflict('source revision changed')
            if current['input_digest'] == catalog.fingerprint:
                return current
            self.db.execute('UPDATE sources SET body=?,digest=?,revision=revision+1 WHERE id=?', (canonical(catalog.document), catalog.fingerprint, key))
        return self.source(key)

    def branch(self, key, revision, target):
        current = self.source(key)
        if current['revision'] != revision:
            raise Conflict('branch source revision changed')
        if target == key:
            raise Conflict('branch must have distinct identity')
        return self.create(target, current['document'])

    def run(self, key, source, revision, as_of, known_at, mode='central', maximum_nodes=500000):
        ident(key, 'run.id')
        integer(revision, 'revision', 1)
        current = self.source(source)
        if current['revision'] != revision:
            raise Conflict('requested source revision is stale')
        request = {'source': source, 'revision': revision, 'as_of': as_of, 'known_at': known_at, 'mode': mode, 'maximum_nodes': maximum_nodes, 'input_digest': current['input_digest']}
        row = self.db.execute('SELECT request FROM runs WHERE id=?', (key,)).fetchone()
        if row:
            if json.loads(row['request']) != request:
                raise Conflict('run identity binds another request')
            return self.result(key)
        catalog = Catalog(current['document'])
        result = analyze(catalog, as_of, known_at, mode, maximum_nodes)
        verify(catalog, result)
        with self.atomic():
            after = self.source(source)
            if after['revision'] != revision or after['input_digest'] != current['input_digest']:
                raise Conflict('source changed during analysis')
            row = self.db.execute('SELECT request FROM runs WHERE id=?', (key,)).fetchone()
            if row and json.loads(row['request']) != request:
                raise Conflict('concurrent run identifier conflict')
            self.db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?)', (key, source, revision, canonical(request), canonical(catalog.document), canonical(result), result['analysis_digest']))
        return self.result(key)

    def result(self, key):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        result = json.loads(row['result'])
        request = json.loads(row['request'])
        catalog = Catalog(json.loads(row['source_body']))
        if result['analysis_digest'] != row['digest'] or catalog.fingerprint != request['input_digest']:
            raise ValueError('run source/result binding mismatch')
        if request['source'] != row['source'] or request['revision'] != row['revision']:
            raise ValueError('run request revision mismatch')
        for field in ('as_of', 'known_at'):
            if result[field] != request[field]:
                raise ValueError('run temporal context mismatch')
        verify(catalog, result)
        return {'id': key, 'request': request, 'result': result}

    def catalog(self, key):
        row = self.db.execute('SELECT source_body FROM runs WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return Catalog(json.loads(row[0]))

    def _review_event(self, run, body):
        previous = self.db.execute('SELECT digest FROM review_events WHERE run=? ORDER BY revision DESC LIMIT 1', (run,)).fetchone()
        prior = previous[0] if previous else None
        record = {**body, 'run': run, 'previous': prior}
        self.db.execute('INSERT INTO review_events VALUES(?,?,?,?,?)', (run, body['revision'], canonical(record), digest(record), prior))

    def propose(self, run, actor):
        text(actor, 'actor')
        result = self.result(run)['result']
        if result['plan']['status'] == 'infeasible':
            raise Conflict('infeasible plan cannot enter procurement review')
        with self.atomic():
            row = self.db.execute('SELECT * FROM reviews WHERE run=?', (run,)).fetchone()
            if row:
                if row['proposer'] != actor:
                    raise Conflict('run already proposed by another actor')
                return self.review(run)
            self.db.execute("INSERT INTO reviews VALUES(?,'pending',1,?,NULL,NULL,?)", (run, actor, result['analysis_digest']))
            self._review_event(run, {'revision': 1, 'state': 'pending', 'actor': actor, 'reason': None, 'analysis_digest': result['analysis_digest']})
        return self.review(run)

    def decide(self, run, revision, actor, approved, reason):
        text(actor, 'actor')
        text(reason, 'reason', 1000)
        integer(revision, 'revision', 1)
        if type(approved) is not bool:
            raise ValueError('approved must be boolean')
        with self.atomic():
            current = self.review(run)
            if current['revision'] != revision or current['state'] != 'pending':
                raise Conflict('review changed or already decided')
            if current['proposer'] == actor:
                raise Conflict('reviewer must differ from proposer')
            state = 'approved' if approved else 'rejected'
            self.db.execute('UPDATE reviews SET state=?,revision=revision+1,reviewer=?,reason=? WHERE run=?', (state, actor, reason, run))
            self._review_event(run, {'revision': revision + 1, 'state': state, 'actor': actor, 'reason': reason, 'analysis_digest': current['run_digest']})
        return self.review(run)

    def review(self, run):
        row = self.db.execute('SELECT * FROM reviews WHERE run=?', (run,)).fetchone()
        if row is None:
            raise KeyError(run)
        output = dict(row)
        if self.result(run)['result']['analysis_digest'] != output['run_digest']:
            raise ValueError('review snapshot binding mismatch')
        history = []
        prior = None
        for expected, event in enumerate(self.db.execute('SELECT * FROM review_events WHERE run=? ORDER BY revision', (run,)), 1):
            body = json.loads(event['body'])
            if event['revision'] != expected or body['revision'] != expected or event['previous'] != prior or body['previous'] != prior or digest(body) != event['digest']:
                raise ValueError('review history integrity failure')
            if body['run'] != run or body['analysis_digest'] != output['run_digest']:
                raise ValueError('review event source binding mismatch')
            history.append(body)
            prior = event['digest']
        if not history or len(history) != output['revision'] or history[-1]['state'] != output['state']:
            raise ValueError('review head differs from history')
        if history[0]['actor'] != output['proposer'] or history[0]['state'] != 'pending':
            raise ValueError('review proposer history mismatch')
        if output['state'] == 'pending':
            if output['revision'] != 1 or output['reviewer'] is not None or output['reason'] is not None:
                raise ValueError('pending review has decision fields')
        elif output['revision'] != 2 or output['reviewer'] == output['proposer'] or output['reviewer'] != history[-1]['actor'] or output['reason'] != history[-1]['reason']:
            raise ValueError('review decision violates separation or history')
        output['history'] = history
        return output

    def publish(self, label, run):
        ident(label, 'label')
        with self.atomic():
            review = self.review(run)
            if review['state'] != 'approved':
                raise Conflict('only approved run can be published')
            row = self.db.execute('SELECT * FROM labels WHERE name=?', (label,)).fetchone()
            if row and (row['run'] != run or row['digest'] != review['run_digest']):
                raise Conflict('publication label is immutable')
            self.db.execute('INSERT OR IGNORE INTO labels VALUES(?,?,?)', (label, run, review['run_digest']))
        return self.publication(label)

    def publication(self, label):
        row = self.db.execute('SELECT * FROM labels WHERE name=?', (label,)).fetchone()
        if row is None:
            raise KeyError(label)
        review = self.review(row['run'])
        if review['state'] != 'approved' or row['digest'] != review['run_digest']:
            raise ValueError('publication review binding mismatch')
        return dict(row)

    def audit(self, replay=True):
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self.db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('workspace SQLite integrity failure')
        sources = [row[0] for row in self.db.execute('SELECT id FROM sources ORDER BY id')]
        runs = [row[0] for row in self.db.execute('SELECT id FROM runs ORDER BY id')]
        reviews = [row[0] for row in self.db.execute('SELECT run FROM reviews ORDER BY run')]
        labels = [row[0] for row in self.db.execute('SELECT name FROM labels ORDER BY name')]
        for key in sources:
            self.source(key)
        for key in runs:
            verify(self.catalog(key), self.result(key)['result'], replay)
        for key in reviews:
            self.review(key)
        for key in labels:
            self.publication(key)
        return {'valid': True, 'sources': len(sources), 'runs': len(runs), 'reviews': len(reviews), 'labels': len(labels), 'replayed': replay}

    def pending_reviews(self, limit=100, after=None):
        integer(limit, 'limit', 1, 1000)
        if after is not None:
            ident(after, 'after')
        keys = [row[0] for row in self.db.execute(
            "SELECT run FROM reviews WHERE state='pending' AND run>? ORDER BY run LIMIT ?",
            (after or '', limit + 1))]
        page = keys[:limit]
        rows = []
        for key in page:
            review = self.review(key)
            result = self.result(key)['result']
            rows.append({'run': key, 'revision': review['revision'],
                         'proposer': review['proposer'], 'as_of': result['as_of'],
                         'plan_status': result['plan']['status'],
                         'cost_cents': result['plan']['objective']['cost_cents'],
                         'analysis_digest': review['run_digest']})
        return {'rows': rows, 'next_after': page[-1] if len(keys) > limit else None}
