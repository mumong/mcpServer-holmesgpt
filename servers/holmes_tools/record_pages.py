"""Bounded, expiring snapshot continuations; never re-query a half-delivered page."""
import copy
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .record_query import encode

PREFIX = 'lr1.'


def fingerprint(arguments):
    return hashlib.sha256(encode({k: v for k, v in arguments.items() if k != 'continue_token'}).encode()).hexdigest()


@contextmanager
def connect():
    path = Path(os.getenv('AIOPS_RECORD_CURSOR_DB', '/tmp/aiops-record-pages/snapshots.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, created REAL, query TEXT, body TEXT, bytes INTEGER)')
    os.chmod(path, 0o600)
    try:
        with db:
            yield db
    finally:
        db.close()


def save(identifier, packet, arguments):
    body = encode(packet)
    size = len(body.encode())
    limit = max(65536, int(os.getenv('AIOPS_RECORD_CURSOR_CACHE_BYTES', '33554432')))
    if size > limit:
        raise ValueError('cursor_snapshot_too_large: reduce page_size; no records were discarded')
    ttl = max(30, int(os.getenv('AIOPS_RECORD_CURSOR_TTL_SECONDS', '600')))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM pages WHERE created < ?', (time.time() - ttl,))
        while db.execute('SELECT COALESCE(SUM(bytes),0) FROM pages').fetchone()[0] + size > limit:
            db.execute('DELETE FROM pages WHERE id = (SELECT id FROM pages ORDER BY created LIMIT 1)')
        db.execute('INSERT INTO pages VALUES (?,?,?,?,?)', (identifier, time.time(), fingerprint(arguments), body, size))


def read(arguments, limit):
    token = arguments.get('continue_token', '')
    match = re.fullmatch(r'lr1\.([0-9a-f]{32})\.([0-9]+)', token)
    if not match:
        raise ValueError('invalid_record_cursor')
    identifier, offset = match.group(1), int(match.group(2))
    ttl = max(30, int(os.getenv('AIOPS_RECORD_CURSOR_TTL_SECONDS', '600')))
    with connect() as db:
        row = db.execute('SELECT created,query,body FROM pages WHERE id=?', (identifier,)).fetchone()
    if row is None or row[0] < time.time() - ttl:
        raise ValueError('record_cursor_expired: restart the query without continue_token; do not combine snapshots')
    if row[1] != fingerprint(arguments):
        raise ValueError('record_cursor_query_changed: keep original scope, fields, filters and page_size')
    packet = json.loads(row[2])
    return slice_packet(packet, arguments, limit, identifier, offset)


def slice_packet(packet, arguments, limit, identifier, offset=0):
    key = 'rows' if packet['result_type'] == 'records' else 'groups'
    rows = packet[key]
    if offset < 0 or (offset >= len(rows) and (rows or offset)):
        raise ValueError('invalid_record_cursor_offset')
    source_next = packet['source_metadata'].get('next_continue_token')

    def page(end):
        result = copy.deepcopy({k: v for k, v in packet.items() if k != key})
        more = end < len(rows)
        token = f'{PREFIX}{identifier}.{end}' if more else source_next
        result[key] = rows[offset:end]
        result['executed_query'] = arguments
        result['coverage'] = 'partial' if offset or more or source_next else packet['coverage']
        result['source_metadata']['next_continue_token'] = token
        result['pagination'] = {'snapshot_id': identifier, 'offset': offset, 'returned_rows': end-offset,
                                'total_rows': len(rows), 'remaining_rows': len(rows)-end,
                                'next_continue_token': token, 'source_has_more': bool(source_next)}
        return result

    complete = page(len(rows))
    if len(encode(complete).encode()) <= limit:
        return complete
    low, high = offset, len(rows)
    while low < high:
        mid = (low + high + 1) // 2
        if len(encode(page(mid)).encode()) <= limit:
            low = mid
        else:
            high = mid - 1
    result = page(low)
    if len(encode(result).encode()) > limit or (low == offset and rows):
        identity = {k: rows[offset].get(k) for k in ('namespace', 'pod', 'uid', 'container') if k in rows[offset]} if rows else {}
        raise ValueError('record_too_large: one complete record or metadata exceeds budget; '
                         'query this Pod without record parameters for detail, or select fewer fields: ' + encode(identity))
    return result


def paginate(packet, arguments, limit):
    if len(encode(packet).encode()) <= limit:
        return packet
    identifier = uuid.uuid4().hex
    result = slice_packet(packet, arguments, limit, identifier)
    save(identifier, packet, arguments)
    return result
