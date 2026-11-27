"""SQLite : une seule connexion protégée par un verrou (le serveur tourne en un seul processus)."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

_lock = threading.RLock()
_conn = None
_settings_json = None      # reglages fusionnes avec les valeurs par defaut, gardes en memoire (un seul processus)

DEFAULTS = {
    'quality': {'cq': 28, 'preset': 'p6', 'tune': 'hq', 'lookahead': 32, 'bframes': 3,
                'cpu_crf': 28, 'cpu_preset': 'medium', 'maxrate_pct': 70},
    'replace_mode': 'replace',        # replace = supprime l'original ; quarantine = le garde dans .quarantaine
    'bad_delete_mode': 'quarantine',  # delete | quarantine
    'rename': {'enabled': True, 'strip_tag': True, 'pad3': False},
    'tolerance_s': 2.0,
    'min_gain_pct': 5,
    # AV1 -> HEVC (compatibilite) : le resultat peut etre plus gros, jusqu'a max_growth_pct % de plus que l'original
    'av1': {'reencode': False, 'max_growth_pct': 50},
    'scan': {'level': 'quick', 'concurrency': 2},
    'sonarr': {'url': '', 'api_key': '', 'unmonitor_done': True},
    'radarr': {'url': '', 'api_key': '', 'unmonitor_done': True},
    'posters': {'tmdb_key': '', 'omdb_key': '', 'language': 'fr-FR'},
    'public_url': '',
    'max_attempts': 2,
    'lease_s': 300,
    'paused': False,
    'setup_done': False,              # assistant de premier lancement termine (ou passe)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, source TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, pw_hash TEXT, created REAL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, username TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS hosts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, token_hash TEXT, enabled INTEGER DEFAULT 1,
  gpus_enabled TEXT, info TEXT, last_seen REAL, created REAL);
CREATE TABLE IF NOT EXISTS enroll_codes(code_hash TEXT PRIMARY KEY, expires REAL, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS episodes(
  path TEXT PRIMARY KEY, series TEXT, size INTEGER, mtime INTEGER, probe TEXT, codec TEXT,
  bad INTEGER DEFAULT 0, issues TEXT DEFAULT '[]', scanned_at REAL);
CREATE INDEX IF NOT EXISTS ep_series ON episodes(series);
CREATE TABLE IF NOT EXISTS series_state(series TEXT PRIMARY KEY, batch INTEGER);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, series TEXT, batch INTEGER, status TEXT,
  host_id INTEGER, gpu TEXT, progress REAL DEFAULT 0, phase TEXT DEFAULT '', speed TEXT DEFAULT '',
  attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', src_size INTEGER, src_mtime INTEGER,
  out_size INTEGER, cancel INTEGER DEFAULT 0, created REAL, started REAL, finished REAL, lease_until REAL);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(path)
  WHERE status IN ('queued','claimed','encoding','uploading','verifying');
CREATE INDEX IF NOT EXISTS jobs_series ON jobs(series, batch);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, batch, id);
CREATE INDEX IF NOT EXISTS jobs_path ON jobs(path, id);
CREATE INDEX IF NOT EXISTS jobs_finished ON jobs(finished);
CREATE INDEX IF NOT EXISTS logs_source ON logs(source);
"""


def init(path):
    global _conn, _settings_json
    _settings_json = None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute('PRAGMA journal_mode=WAL')
    _conn.executescript(SCHEMA)
    cols = {r[1] for r in _conn.execute('PRAGMA table_info(jobs)')}
    if 'claim_token' not in cols:     # jeton de reservation (hub 1.11.1)
        _conn.execute('ALTER TABLE jobs ADD COLUMN claim_token TEXT')
    if 'agent_update' not in {r[1] for r in _conn.execute('PRAGMA table_info(hosts)')}:   # hub 1.12.0
        _conn.execute('ALTER TABLE hosts ADD COLUMN agent_update TEXT')
    if 'reusable' not in {r[1] for r in _conn.execute('PRAGMA table_info(enroll_codes)')}:   # hub 1.13.0
        # cle reutilisable (machine non persistante) : rattachee au host qu'elle a cree
        _conn.execute('ALTER TABLE enroll_codes ADD COLUMN reusable INTEGER DEFAULT 0')
        _conn.execute('ALTER TABLE enroll_codes ADD COLUMN host_id INTEGER')


@contextmanager
def tx():
    """Transaction atomique (BEGIN IMMEDIATE) : sert à réserver un épisode sans doublon."""
    with _lock:
        _conn.execute('BEGIN IMMEDIATE')
        try:
            yield _conn
            _conn.execute('COMMIT')
        except BaseException:
            _conn.execute('ROLLBACK')
            raise


def q(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid


def get_settings():
    """Reglages fusionnes avec les valeurs par defaut. Lus tres souvent (chaque signe de vie d'un agent, chaque
    rafraichissement de l'interface) : la base n'est relue qu'apres une modification. Chaque appel renvoie sa
    propre copie, que l'appelant peut modifier."""
    global _settings_json
    with _lock:
        if _settings_json is None:
            out = json.loads(json.dumps(DEFAULTS))
            for r in q('SELECT key,value FROM settings'):
                v = json.loads(r['value'])
                if isinstance(out.get(r['key']), dict) and isinstance(v, dict):
                    out[r['key']].update(v)
                elif r['key'] in out:
                    out[r['key']] = v
            _settings_json = json.dumps(out)
        return json.loads(_settings_json)


def has_setting(key):
    return one('SELECT 1 FROM settings WHERE key=?', (key,)) is not None


def set_settings(patch):
    global _settings_json
    with _lock:
        cur = get_settings()
        try:
            for k, v in patch.items():
                if k not in DEFAULTS:
                    continue
                if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
                    merged = dict(cur[k])
                    merged.update({a: b for a, b in v.items() if a in DEFAULTS[k]})
                    v = merged
                run('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (k, json.dumps(v)))
        finally:
            _settings_json = None
