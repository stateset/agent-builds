"""Exact named resources, acquired together under Store's write transaction."""
import json


def normalize(primary, extra=None):
    if extra is None:
        extra = []
    if not isinstance(extra, list) or len(extra) > 15:
        raise ValueError('locks must be a list of at most 15 additional resource keys')
    keys = [primary] + extra
    if any(not isinstance(key, str) or not key.strip() or '\0' in key or len(key) > 4096 for key in keys):
        raise ValueError('Resource keys must contain 1 to 4096 characters without NUL')
    return list(dict.fromkeys(keys))


def keys(row):
    """Legacy rows declare only their primary key. Never infer missing locks."""
    primary = row['resource_key']
    if not row.get('lock_protocol'):
        return [primary]
    value = row.get('resource_keys') or []
    return json.loads(value) if isinstance(value, str) else value


def overlaps(left, right):
    return bool(set(keys(left)).intersection(keys(right)))


def eligible(queued):
    """Held multi-lock work drains overlapping old queued workers first.

    Old workers cannot see held work, so must not be blocked behind it. New
    claims and legacy inserts are serialized by the same SQLite transaction.
    """
    legacy = [row for row in queued if not row.get('lock_protocol')]
    return [row for row in queued if len(keys(row)) == 1 or
            not any(overlaps(row, old) for old in legacy)]
