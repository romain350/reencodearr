"""Authentification : mots de passe scrypt, sessions par cookie, jetons d'agents."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from . import db

SESSION_TTL = 7 * 86400
MAX_PASSWORD = 1024       # au-dela, scrypt ne sert qu'a occuper le serveur
_fails = {}          # ip -> horodatages des échecs récents
_fails_lock = threading.Lock()
_dummy = []          # empreinte factice, calculee une fois (voir login)


def hash_password(pw, salt=None):
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + ':' + h.hex()


def check_password(pw, stored):
    try:
        salt, h = stored.split(':')
        new = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(new.hex(), h)
    except Exception:
        return False


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


USERNAME = re.compile(r'[A-Za-z0-9._@-]{1,40}')


def has_users():
    return db.one('SELECT 1 FROM users') is not None


def ensure_admin(user, password):
    """Compatibilite : cree le premier compte depuis ADMIN_USER / ADMIN_PASSWORD s'ils sont fournis.
    Sans mot de passe, rien n'est cree : l'assistant de premier lancement le demandera dans le navigateur."""
    if not password or has_users():
        return False
    return create_first_admin(user, password) is None


def create_first_admin(user, password):
    """Cree le compte admin depuis l'assistant, seulement s'il n'existe encore aucun compte.
    Renvoie None si c'est fait, sinon le message d'erreur."""
    if not USERNAME.fullmatch(user or ''):
        return "nom d'utilisateur invalide (lettres, chiffres, . _ @ -, 40 caracteres maximum)"
    if len(password or '') < 8:
        return 'mot de passe trop court (8 caracteres minimum)'
    if len(password) > MAX_PASSWORD:
        return 'mot de passe trop long (%d caracteres maximum)' % MAX_PASSWORD
    with db.tx() as c:
        if c.execute('SELECT 1 FROM users').fetchone():
            return 'un compte administrateur existe deja'
        c.execute('INSERT INTO users(username,pw_hash,created) VALUES(?,?,?)',
                  (user, hash_password(password), time.time()))
    return None


def too_many_failures(ip):
    now = time.time()
    with _fails_lock:
        for k in [k for k, v in _fails.items() if not v or now - v[-1] >= 300]:   # la table ne grossit pas sans fin
            _fails.pop(k, None)
        return len([t for t in _fails.get(ip, []) if now - t < 300]) >= 5


def login(username, password, ip):
    if too_many_failures(ip):
        return None, 'trop'
    row = db.one('SELECT pw_hash FROM users WHERE username=?', (username,))
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(8)))
    # compte inconnu : meme travail (un seul scrypt) que pour un compte connu, le temps de reponse ne dit pas
    # si le nom d'utilisateur existe
    ok = len(password) <= MAX_PASSWORD and check_password(password, row['pw_hash'] if row else _dummy[0])
    if not row or not ok:
        with _fails_lock:
            _fails.setdefault(ip, []).append(time.time())
        return None, 'refus'
    return open_session(username), None


def open_session(username):
    token = secrets.token_urlsafe(32)
    db.run('DELETE FROM sessions WHERE expires<?', (time.time(),))      # menage des sessions expirees
    db.run('INSERT INTO sessions(token_hash,username,expires) VALUES(?,?,?)',
           (sha(token), username, time.time() + SESSION_TTL))
    return token


def session_user(token):
    if not token:
        return None
    row = db.one('SELECT username,expires FROM sessions WHERE token_hash=?', (sha(token),))
    if not row or row['expires'] < time.time():
        return None
    return row['username']


def logout(token):
    if token:
        db.run('DELETE FROM sessions WHERE token_hash=?', (sha(token),))


def change_password(username, old, new):
    row = db.one('SELECT pw_hash FROM users WHERE username=?', (username,))
    if not row or not 8 <= len(new) <= MAX_PASSWORD or len(old) > MAX_PASSWORD or not check_password(old, row['pw_hash']):
        return False
    db.run('UPDATE users SET pw_hash=? WHERE username=?', (hash_password(new), username))
    db.run('DELETE FROM sessions WHERE username=?', (username,))
    return True


REUSABLE_FIRST_USE = 7 * 86400     # une cle reutilisable doit servir une premiere fois dans les 7 jours


def new_enroll_code(reusable=False):
    """Code d'enrolement. Ordinaire : une seule fois, pendant 1 h. Reutilisable (machine non persistante qui se
    reinstalle a chaque demarrage) : premiere utilisation sous 7 jours, puis sans limite ; il reste rattache au host
    qu'il a cree, et retirer ce host le revoque."""
    code = secrets.token_urlsafe(18 if not reusable else 24)
    while code[0] in '-_':      # « agent.py register --code -abc » : un tiret en tete serait pris pour une option
        code = secrets.token_urlsafe(18 if not reusable else 24)
    # menage : codes ordinaires utilises ou expires, cles reutilisables jamais servies et expirees
    db.run('DELETE FROM enroll_codes WHERE host_id IS NULL AND (used=1 OR expires<?)', (time.time() - 86400,))
    db.run('INSERT INTO enroll_codes(code_hash,expires,reusable) VALUES(?,?,?)',
           (sha(code), time.time() + (REUSABLE_FIRST_USE if reusable else 3600), 1 if reusable else 0))
    return code


def code_valid(code):
    row = db.one('SELECT expires,used,reusable,host_id FROM enroll_codes WHERE code_hash=?', (sha(code),))
    return bool(row and not row['used'] and (row['host_id'] or row['expires'] > time.time()))


def consume_code(code):
    """None si le code est refuse, sinon {'reusable': bool, 'host_id': host deja cree par cette cle ou None}.
    Un code ordinaire est marque utilise ; une cle reutilisable reste valable."""
    with db.tx() as c:
        row = c.execute('SELECT expires,used,reusable,host_id FROM enroll_codes WHERE code_hash=?',
                        (sha(code),)).fetchone()
        if not row or row['used'] or (not row['host_id'] and row['expires'] < time.time()):
            return None
        if not row['reusable']:
            c.execute('UPDATE enroll_codes SET used=1 WHERE code_hash=?', (sha(code),))
        return {'reusable': bool(row['reusable']), 'host_id': row['host_id']}


def bind_code(code, host_id):
    """Rattache une cle reutilisable au host qu'elle vient de creer."""
    db.run('UPDATE enroll_codes SET host_id=? WHERE code_hash=? AND reusable=1', (host_id, sha(code)))


def new_host_token():
    return secrets.token_urlsafe(32)
