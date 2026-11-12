import copy
from equipment_domain.values import fields, text, ident, integer, boolean, day, amount, rows, index, string_set, digest, Invalid


class Catalog:
    def __init__(self, document):
        self.document = copy.deepcopy(document)
        fields(self.document, ('version', 'sites', 'classes', 'assets', 'inspections', 'maintenance', 'proposals', 'periods', 'policy'))
        if type(self.document['version']) is not int or self.document['version'] != 1:
            raise Invalid('version', 'version 1 required')
        self.sites = index(self.document['sites'], 'sites')
        self.classes = index(self.document['classes'], 'classes')
        self.assets = index(self.document['assets'], 'assets')
        self.inspections = index(self.document['inspections'], 'inspections')
        self.maintenance = index(self.document['maintenance'], 'maintenance')
        self.proposals = index(self.document['proposals'], 'proposals')
        self.periods = index(self.document['periods'], 'periods')
        self.policy = self.document['policy']
        self._sites()
        self._classes()
        self._assets()
        self._inspections()
        self._maintenance()
        self._periods()
        self._proposals()
        from risk_model.policy import validate
        validate(self.policy)
        self.fingerprint = digest(self.document)

    def _sites(self):
        for row in self.sites.values():
            fields(row, ('id', 'region', 'rural', 'users', 'minimum_operating'))
            ident(row['region'], 'site.region')
            boolean(row['rural'], 'site.rural')
            integer(row['users'], 'site.users', 0, 1000000)
            if not isinstance(row['minimum_operating'], dict):
                raise Invalid('minimum_operating', 'object required')
            for key, value in row['minimum_operating'].items():
                if key not in self.classes:
                    raise Invalid('minimum_operating', 'unknown class')
                integer(value, 'minimum_operating.value', 0, 10000)

    def _classes(self):
        for row in self.classes.values():
            fields(row, ('id', 'design_life_days', 'inspection_valid_days', 'criticality', 'checks'))
            integer(row['design_life_days'], 'class.design_life_days', 1, 36600)
            integer(row['inspection_valid_days'], 'class.inspection_valid_days', 1, 3660)
            integer(row['criticality'], 'class.criticality', 1, 10)
            checks = string_set(row['checks'], 'class.checks', 30)
            if not checks:
                raise Invalid('class.checks', 'at least one check required')

    def _assets(self):
        serials = set()
        for row in self.assets.values():
            fields(row, ('id', 'serial', 'site', 'class', 'parent', 'commissioned', 'retired', 'utilization_pct', 'redundancy', 'outage_cost_daily'))
            text(row['serial'], 'asset.serial')
            if row['serial'] in serials:
                raise Invalid('asset.serial', 'duplicate serial')
            serials.add(row['serial'])
            if row['site'] not in self.sites or row['class'] not in self.classes:
                raise Invalid('asset', 'unknown site or class')
            start = day(row['commissioned'], 'asset.commissioned')
            if row['retired'] is not None and day(row['retired'], 'asset.retired') <= start:
                raise Invalid('asset.retired', 'must follow commissioning')
            integer(row['utilization_pct'], 'asset.utilization_pct', 0, 100)
            integer(row['redundancy'], 'asset.redundancy', 0, 100)
            amount(row['outage_cost_daily'], 'asset.outage_cost_daily')
            parent = row['parent']
            if parent is not None:
                if parent not in self.assets or parent == row['id']:
                    raise Invalid('asset.parent', 'unknown or self parent')
                if self.assets[parent]['site'] != row['site']:
                    raise Invalid('asset.parent', 'component must share parent site')
        for key in self.assets:
            seen = set()
            current = key
            while current is not None:
                if current in seen:
                    raise Invalid('asset.parent', 'cycle in component hierarchy')
                seen.add(current)
                current = self.assets[current]['parent']

    def _inspections(self):
        for row in self.inspections.values():
            fields(row, ('id', 'asset', 'performed', 'recorded', 'inspector', 'checks'))
            if row['asset'] not in self.assets:
                raise Invalid('inspection.asset', 'unknown asset')
            performed = day(row['performed'], 'inspection.performed')
            if performed < day(self.assets[row['asset']]['commissioned'], 'commissioned'):
                raise Invalid('inspection', 'precedes commissioning')
            if day(row['recorded'], 'inspection.recorded') < performed:
                raise Invalid('inspection', 'recording precedes performance')
            text(row['inspector'], 'inspection.inspector')
            allowed = set(self.classes[self.assets[row['asset']]['class']]['checks'])
            if not isinstance(row['checks'], dict) or set(row['checks']) - allowed:
                raise Invalid('inspection.checks', 'unknown checks')
            for value in row['checks'].values():
                if value is not None:
                    integer(value, 'inspection.severity', 0, 4)

    def _maintenance(self):
        for row in self.maintenance.values():
            fields(row, ('id', 'asset', 'start', 'end', 'recorded', 'kind', 'cost', 'restores_checks'))
            if row['asset'] not in self.assets:
                raise Invalid('maintenance.asset', 'unknown asset')
            start = day(row['start'], 'maintenance.start')
            end = day(row['end'], 'maintenance.end')
            if end <= start or (end - start).days > 366:
                raise Invalid('maintenance', 'positive bounded half-open interval required')
            if day(row['recorded'], 'maintenance.recorded') < end:
                raise Invalid('maintenance.recorded', 'only completed maintenance may be recorded')
            if row['kind'] not in ('preventive', 'repair', 'recall'):
                raise Invalid('maintenance.kind', 'unknown kind')
            amount(row['cost'], 'maintenance.cost')
            checks = string_set(row['restores_checks'], 'maintenance.restores_checks')
            allowed = set(self.classes[self.assets[row['asset']]['class']]['checks'])
            if checks - allowed:
                raise Invalid('maintenance.restores_checks', 'unknown class checks')

    def _periods(self):
        spans = []
        for row in self.periods.values():
            fields(row, ('id', 'start', 'end', 'budget', 'crew_days'))
            start = day(row['start'], 'period.start')
            end = day(row['end'], 'period.end')
            if start >= end:
                raise Invalid('period', 'positive half-open interval required')
            amount(row['budget'], 'period.budget')
            integer(row['crew_days'], 'period.crew_days', 0, 100000)
            spans.append((start, end))
        ordered = sorted(spans)
        if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
            raise Invalid('periods', 'funding periods must not overlap')

    def _proposals(self):
        if len(self.proposals) > 22:
            raise Invalid('proposals', 'exact portfolio search supports at most 22 candidates')
        from equipment_domain.values import ratio
        for row in self.proposals.values():
            fields(row, ('id', 'asset', 'action', 'period', 'cost', 'crew_days', 'outage_days', 'reduction', 'requires', 'excludes', 'mandatory'))
            if row['asset'] not in self.assets or row['period'] not in self.periods:
                raise Invalid('proposal', 'unknown asset or period')
            if row['action'] not in ('replace', 'retrofit', 'inspect'):
                raise Invalid('proposal.action', 'unsupported action')
            amount(row['cost'], 'proposal.cost')
            integer(row['crew_days'], 'proposal.crew_days', 0, 10000)
            integer(row['outage_days'], 'proposal.outage_days', 0, 366)
            ratio(row['reduction'], 'proposal.reduction')
            boolean(row['mandatory'], 'proposal.mandatory')
            for field in ('requires', 'excludes'):
                related = string_set(row[field], 'proposal.' + field)
                if row['id'] in related or related - self.proposals.keys():
                    raise Invalid('proposal.' + field, 'unknown or self reference')
            if set(row['requires']) & set(row['excludes']):
                raise Invalid('proposal', 'direct requirement/exclusion contradiction')
        from funding.constraints import dependency_order
        dependency_order(self)

    def export(self):
        return copy.deepcopy(self.document)
