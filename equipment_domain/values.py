from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import re


class Invalid(ValueError):
    def __init__(self, field, reason):
        self.field = field
        super().__init__(f'{field}: {reason}')


def fields(value, required, optional=(), path='$'):
    if not isinstance(value, dict):
        raise Invalid(path, 'object required')
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        raise Invalid(path, f'missing={sorted(missing)} extra={sorted(extra)}')
    return value


def text(value, path, maximum=120):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Invalid(path, 'nonempty bounded string required')
    if any(ord(char) < 32 for char in value):
        raise Invalid(path, 'control characters forbidden')
    return value


def ident(value, path):
    text(value, path, 80)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise Invalid(path, 'ASCII identifier required')
    return value


def integer(value, path, low=0, high=10**12):
    if type(value) is not int or not low <= value <= high:
        raise Invalid(path, f'integer in [{low},{high}] required')
    return value


def boolean(value, path):
    if type(value) is not bool:
        raise Invalid(path, 'boolean required')
    return value


def day(value, path):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise Invalid(path, 'ISO calendar date required')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise Invalid(path, 'invalid date') from exc
    if not 2000 <= parsed.year <= 2100:
        raise Invalid(path, 'date outside 2000..2100')
    return parsed


def after(value, days):
    integer(days, 'days', -36600, 36600)
    result = day(value, 'date') + timedelta(days=days)
    day(result.isoformat(), 'result')
    return result.isoformat()


def amount(value, path):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,10})\.[0-9]{2}', value):
        raise Invalid(path, 'nonnegative decimal money with exactly two digits required')
    try:
        cents = Decimal(value) * 100
    except InvalidOperation as exc:
        raise Invalid(path, 'invalid decimal') from exc
    return int(cents)


def money(cents):
    integer(cents, 'cents')
    return f'{cents // 100}.{cents % 100:02}'


def ratio(value, path, low=Fraction(0), high=Fraction(1)):
    fields(value, ('numerator', 'denominator'), path=path)
    integer(value['numerator'], path + '.numerator', -10**9, 10**9)
    integer(value['denominator'], path + '.denominator', 1, 10**9)
    result = Fraction(value['numerator'], value['denominator'])
    if not low <= result <= high:
        raise Invalid(path, 'ratio outside range')
    return result


def exact(value):
    value = Fraction(value)
    return {'numerator': value.numerator, 'denominator': value.denominator}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def rows(value, path, maximum=10000):
    if not isinstance(value, list) or len(value) > maximum:
        raise Invalid(path, 'bounded array required')
    return value


def index(values, path):
    output = {}
    for value in rows(values, path):
        if not isinstance(value, dict) or 'id' not in value:
            raise Invalid(path, 'each row needs id')
        key = ident(value['id'], path + '.id')
        if key in output:
            raise Invalid(path, 'duplicate id: ' + key)
        output[key] = value
    return output


def string_set(values, path, maximum=100):
    rows(values, path, maximum)
    for value in values:
        ident(value, path)
    if len(values) != len(set(values)):
        raise Invalid(path, 'duplicate identifiers')
    return set(values)


def bounded_file(path, maximum=20_000_000):
    from pathlib import Path
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > maximum:
        raise Invalid('file', 'bounded regular file required')
    return source


def load(path):
    return json.loads(bounded_file(path).read_text(encoding='utf-8'))
