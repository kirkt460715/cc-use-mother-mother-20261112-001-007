import csv
import io
import json
from pathlib import Path
from equipment_domain.catalog import Catalog
from equipment_domain.values import canonical, bounded_file, amount, integer


TABLES = {
    'sites': [('id', 's'), ('region', 's'), ('rural', 'b'), ('users', 'i'), ('minimum_operating', 'j')],
    'classes': [('id', 's'), ('design_life_days', 'i'), ('inspection_valid_days', 'i'), ('criticality', 'i'), ('checks', 'j')],
    'assets': [('id', 's'), ('serial', 's'), ('site', 's'), ('class', 's'), ('parent', 'n'), ('commissioned', 's'), ('retired', 'n'), ('utilization_pct', 'i'), ('redundancy', 'i'), ('outage_cost_daily', 's')],
    'inspections': [('id', 's'), ('asset', 's'), ('performed', 's'), ('recorded', 's'), ('inspector', 's'), ('checks', 'j')],
    'maintenance': [('id', 's'), ('asset', 's'), ('start', 's'), ('end', 's'), ('recorded', 's'), ('kind', 's'), ('cost', 's'), ('restores_checks', 'j')],
    'proposals': [('id', 's'), ('asset', 's'), ('action', 's'), ('period', 's'), ('cost', 's'), ('crew_days', 'i'), ('outage_days', 'i'), ('reduction', 'j'), ('requires', 'j'), ('excludes', 'j'), ('mandatory', 'b')],
    'periods': [('id', 's'), ('start', 's'), ('end', 's'), ('budget', 's'), ('crew_days', 'i')],
}


def parse(value, kind):
    if kind == 's':
        return value
    if kind == 'n':
        return value or None
    if kind == 'b':
        if value not in ('true', 'false'):
            raise ValueError('CSV boolean requires true or false')
        return value == 'true'
    if kind == 'i':
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError('CSV integer requires unsigned ASCII digits')
        return int(value)
    if kind == 'j':
        return json.loads(value)
    raise ValueError('unknown CSV type')


def read_table(table, source):
    if table not in TABLES or not isinstance(source, str) or len(source.encode()) > 20_000_000:
        raise ValueError('unknown table or excessive CSV')
    try:
        reader = csv.reader(io.StringIO(source), strict=True)
        header = next(reader, None)
        schema = dict(TABLES[table])
        if not header or len(header) != len(set(header)) or set(header) != set(schema):
            raise ValueError('CSV header mismatch')
        output = []
        for cells in reader:
            if len(cells) != len(header):
                raise ValueError('CSV record width mismatch')
            output.append({key: parse(cell, schema[key]) for key, cell in zip(header, cells)})
            if len(output) > 10000:
                raise ValueError('CSV row limit exceeded')
        return output
    except csv.Error as exc:
        raise ValueError('malformed CSV') from exc


def write_table(catalog, table):
    if table not in TABLES:
        raise ValueError('unknown CSV table')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow([key for key, kind in TABLES[table]])
    for row in catalog.document[table]:
        cells = []
        for key, kind in TABLES[table]:
            value = row[key]
            if kind == 'j':
                value = canonical(value)
            elif kind == 'b':
                value = 'true' if value else 'false'
            elif value is None:
                value = ''
            cells.append(value)
        writer.writerow(cells)
    return output.getvalue()


def export_directory(catalog, directory):
    target = Path(directory)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.mkdir(parents=True)
    written = []
    try:
        for table in TABLES:
            path = target / (table + '.csv')
            path.write_text(write_table(catalog, table), encoding='utf-8')
            written.append(path)
        path = target / 'policy.json'
        path.write_text(canonical(catalog.policy), encoding='utf-8')
        written.append(path)
    except BaseException:
        for path in written:
            path.unlink()
        target.rmdir()
        raise
    return {'input_digest': catalog.fingerprint, 'files': sorted(path.name for path in written)}


def import_directory(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('regular CSV directory required')
    expected = {table + '.csv' for table in TABLES} | {'policy.json'}
    if {path.name for path in root.iterdir()} != expected:
        raise ValueError('CSV directory file set mismatch')
    document = {'version': 1}
    for table in TABLES:
        document[table] = read_table(table, bounded_file(root / (table + '.csv')).read_text(encoding='utf-8-sig'))
    document['policy'] = json.loads(bounded_file(root / 'policy.json').read_text(encoding='utf-8'))
    return Catalog(document)


def patch_catalog(catalog, changes):
    from equipment_domain.values import fields, rows
    import copy
    rows(changes, 'changes', 1000)
    document = catalog.export()
    touched = set()
    for change in changes:
        fields(change, ('table', 'id', 'before_digest', 'after'))
        table, key = change['table'], change['id']
        if table not in TABLES or (table, key) in touched:
            raise ValueError('unknown or duplicate patch target')
        touched.add((table, key))
        current = next((row for row in document[table] if row['id'] == key), None)
        from equipment_domain.values import digest
        signature = digest(current) if current is not None else None
        if signature != change['before_digest']:
            raise ValueError('patch optimistic digest mismatch')
        after = change['after']
        if after is not None and (not isinstance(after, dict) or after.get('id') != key):
            raise ValueError('replacement row must preserve target id')
        if current is None and after is None:
            raise ValueError('cannot delete nonexistent row')
        if current is not None:
            position = document[table].index(current)
            if after is None:
                document[table].pop(position)
            else:
                document[table][position] = copy.deepcopy(after)
        else:
            document[table].append(copy.deepcopy(after))
    return Catalog(document)
