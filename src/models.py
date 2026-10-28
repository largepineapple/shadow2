import hashlib
import json
from datetime import datetime, timezone


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('deadline must include UTC offset')
    return result.astimezone(timezone.utc)


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {'request_id', 'theme', 'budget', 'deadline'}:
        raise ValueError('Unexpected request fields')
    for key in ('request_id', 'theme'):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ValueError(key + ' must be a nonempty string')
    missing = [key for key in ('budget', 'deadline') if request.get(key) in (None, '')]
    normalized = dict(request)
    if 'budget' not in missing:
        if type(request['budget']) is not int or request['budget'] < 0:
            raise ValueError('budget must be nonnegative integer simulated units')
    if 'deadline' not in missing:
        normalized['deadline'] = parse_time(request['deadline']).isoformat()
    return normalized, missing


def fingerprint(request):
    return hashlib.sha256(canonical(request).encode()).hexdigest()
