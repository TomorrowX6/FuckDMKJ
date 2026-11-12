"""Private SQLite state, per-account grants and durable job transitions."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from dm_device import simulated_ua

TERMINAL = frozenset({'success', 'failed', 'cancelled', 'uncertain'})


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts(uid TEXT PRIMARY KEY, profile TEXT NOT NULL,
                    standard_ua TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS grants(uid TEXT NOT NULL, tg_id INTEGER NOT NULL,
                    PRIMARY KEY(uid,tg_id), FOREIGN KEY(uid) REFERENCES accounts(uid));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, uid TEXT NOT NULL,
                    activity_id TEXT NOT NULL, chat_id INTEGER NOT NULL, creator INTEGER NOT NULL,
                    title TEXT NOT NULL, open_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                    phase TEXT NOT NULL DEFAULT 'waiting', cancel_requested INTEGER NOT NULL DEFAULT 0,
                    submit_attempts INTEGER NOT NULL DEFAULT 0, result TEXT, notified INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    FOREIGN KEY(uid) REFERENCES accounts(uid));
                CREATE UNIQUE INDEX IF NOT EXISTS active_job ON jobs(uid,activity_id)
                    WHERE status IN ('waiting','running','cancelling');
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS login_devices(account_key TEXT PRIMARY KEY,
                    standard_ua TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS login_grants(tg_id INTEGER PRIMARY KEY);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def account(self, uid):
        with self.connection() as db:
            row = db.execute('SELECT * FROM accounts WHERE uid=?', (uid,)).fetchone()
        if row is None:
            raise ValueError('此 UID 尚未导入，请联系主人使用 /login')
        return json.loads(row['profile']), row['standard_ua']

    def put_account(self, uid, profile, standard_ua, *, controller=None):
        with self.connection() as db:
            db.execute('''INSERT INTO accounts VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET
                          profile=excluded.profile,standard_ua=excluded.standard_ua,updated=excluded.updated''',
                       (uid, json.dumps(profile), standard_ua, time.time()))
            if controller is not None:
                db.execute('INSERT OR IGNORE INTO grants VALUES(?,?)', (uid, controller))

    def can_login(self, tg_id, owner):
        if tg_id == owner:
            return True
        with self.connection() as db:
            return db.execute('SELECT 1 FROM login_grants WHERE tg_id=?', (tg_id,)).fetchone() is not None

    def grant_login(self, tg_id):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO login_grants VALUES(?)', (tg_id,))

    def login_device(self, account):
        account_key = hashlib.sha256(account.strip().encode('utf-8')).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT standard_ua FROM login_devices WHERE account_key=?', (account_key,)).fetchone()
            ua = simulated_ua(json.loads(row[0]) if row else None)
            serialized = json.dumps(ua, ensure_ascii=False, separators=(',', ':'))
            db.execute('''INSERT INTO login_devices VALUES(?,?) ON CONFLICT(account_key)
                          DO UPDATE SET standard_ua=excluded.standard_ua''', (account_key, serialized))
        return serialized

    def allowed(self, uid, tg_id, owner):
        if tg_id == owner:
            return True
        with self.connection() as db:
            return db.execute('SELECT 1 FROM grants WHERE uid=? AND tg_id=?', (uid, tg_id)).fetchone() is not None

    def grant(self, uid, tg_id):
        self.account(uid)
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO grants VALUES(?,?)', (uid, tg_id))

    def add_job(self, uid, activity_id, chat_id, creator, title, open_at):
        now = time.time()
        job_id = uuid.uuid4().hex
        with self.connection() as db:
            try:
                db.execute('''INSERT INTO jobs(id,uid,activity_id,chat_id,creator,title,open_at,created,updated)
                              VALUES(?,?,?,?,?,?,?,?,?)''', (job_id, uid, activity_id, chat_id, creator,
                                                           title, open_at, now, now))
            except sqlite3.IntegrityError:
                row = db.execute("SELECT * FROM jobs WHERE uid=? AND activity_id=? AND status IN ('waiting','running','cancelling')",
                                 (uid, activity_id)).fetchone()
                if row is None:
                    raise
                return dict(row), False
        return self.job(job_id), True

    def job(self, job_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def jobs(self, statuses):
        marks = ','.join('?' for _ in statuses)
        with self.connection() as db:
            rows = db.execute(f'SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY open_at,created', tuple(statuses)).fetchall()
        return [dict(row) for row in rows]

    def claim(self, job_id):
        with self.connection() as db:
            result = db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status='waiting' AND cancel_requested=0",
                                (time.time(), job_id))
            return result.rowcount == 1

    def phase(self, job_id, phase):
        with self.connection() as db:
            db.execute('UPDATE jobs SET phase=?,updated=? WHERE id=?', (phase, time.time(), job_id))

    def begin_submit(self, job_id):
        with self.connection() as db:
            result = db.execute("""UPDATE jobs SET phase='submitting',submit_attempts=submit_attempts+1,updated=?
                                   WHERE id=? AND cancel_requested=0 AND status='running'""", (time.time(), job_id))
            return result.rowcount == 1

    def cancelled(self, job_id):
        job = self.job(job_id)
        return job is None or bool(job['cancel_requested'])

    def cancel(self, uid, activity_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE uid=? AND activity_id=? AND status IN ('waiting','running','cancelling')",
                             (uid, activity_id)).fetchone()
            if row is None:
                return None
            status = 'cancelled' if row['status'] == 'waiting' else 'cancelling'
            db.execute('UPDATE jobs SET cancel_requested=1,status=?,updated=? WHERE id=?',
                       (status, time.time(), row['id']))
        return self.job(row['id'])

    def finish(self, job_id, status, result):
        if status not in TERMINAL:
            raise ValueError('Invalid terminal status')
        with self.connection() as db:
            db.execute('UPDATE jobs SET status=?,phase=?,result=?,updated=? WHERE id=?',
                       (status, status, json.dumps(result, ensure_ascii=False), time.time(), job_id))

    def recover(self):
        for job in self.jobs(('running', 'cancelling')):
            if job['submit_attempts']:
                self.finish(job['id'], 'uncertain', {'message': '服务重启前可能已提交，未自动重复报名；请在 App 核对'})
            elif job['cancel_requested']:
                self.finish(job['id'], 'cancelled', {'message': '任务已取消'})
            else:
                with self.connection() as db:
                    db.execute("UPDATE jobs SET status='waiting',phase='waiting',updated=? WHERE id=?", (time.time(), job['id']))

    def unnotified(self):
        return [job for job in self.jobs(tuple(TERMINAL)) if not job['notified']]

    def notified(self, job_id):
        with self.connection() as db:
            db.execute('UPDATE jobs SET notified=1 WHERE id=?', (job_id,))

    def offset(self, value=None):
        with self.connection() as db:
            if value is not None:
                db.execute("INSERT INTO meta VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
            row = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row['value']) if row else 0

    def offset_initialized(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM meta WHERE key='offset'").fetchone() is not None
