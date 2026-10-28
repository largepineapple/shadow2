import json
import sqlite3
from contextlib import contextmanager
from .models import canonical, fingerprint


class Storage:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                request_json TEXT NOT NULL, mode TEXT NOT NULL, active_strategy TEXT);
            CREATE TABLE IF NOT EXISTS steps (
                request_id TEXT NOT NULL, step_key TEXT NOT NULL, tool TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT, error_json TEXT,
                cost INTEGER NOT NULL DEFAULT 0, seconds INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(request_id,step_key));
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                content_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS injected_failures (
                request_id TEXT NOT NULL, tool TEXT NOT NULL,
                PRIMARY KEY(request_id,tool));
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def bind(self, request, mode):
        with self.transaction() as db:
            old = db.execute('SELECT * FROM jobs WHERE request_id=?', (request['request_id'],)).fetchone()
            if old and (old['fingerprint'] != fingerprint(request) or old['mode'] != mode):
                raise ValueError('REQUEST_CONFLICT: request_id is bound to different content or execution mode')
            db.execute('INSERT OR IGNORE INTO jobs(request_id,fingerprint,request_json,mode) VALUES (?,?,?,?)',
                       (request['request_id'], fingerprint(request), canonical(request), mode))

    def steps(self, request_id):
        rows = self.db.execute('SELECT * FROM steps WHERE request_id=? ORDER BY rowid', (request_id,))
        result = []
        for row in rows:
            item = dict(row)
            item['result'] = json.loads(item.pop('result_json') or 'null')
            item['error'] = json.loads(item.pop('error_json') or 'null')
            result.append(item)
        return result

    def saved(self, request_id):
        row = self.db.execute('SELECT * FROM plans WHERE request_id=?', (request_id,)).fetchone()
        return {'plan_id': row['id'], 'content': json.loads(row['content_json'])} if row else None

    def close(self):
        self.db.close()
