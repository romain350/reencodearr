"""Durcissement (hub 1.14.2) :
  - en-tetes : politique de contenu (CSP) partout, reponses de l'API jamais mises en cache ;
  - corps JSON : objet attendu (400 sinon), taille bornee (413), y compris sur les routes sans connexion ;
  - ce qu'un agent declare (GPU, version) est ramene a des types simples avant d'etre affiche ;
  - un resultat d'encodage demesure est refuse avant d'etre ecrit sur le disque ;
  - une adresse d'affiche qui n'est pas en http(s) n'est jamais ouverte ;
  - un code d'enrolement ne commence jamais par un tiret (il casserait « agent.py register --code ») ;
  - les reglages gardes en memoire suivent chaque modification.

Lancer depuis le dossier du projet : python tests/test_securite.py
"""
import http.client
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18097
URL = 'http://127.0.0.1:%d' % PORT
TMP = tempfile.mkdtemp(prefix='rh-secu-')
os.makedirs(os.path.join(TMP, 'series'))
os.environ.update(DATA_DIR=os.path.join(TMP, 'data'), SERIES_DIR=os.path.join(TMP, 'series'),
                  MOVIES_DIR=os.path.join(TMP, 'films'))
sys.path.insert(0, ROOT)
from app import db, journal, main, posters, security  # noqa: E402

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
hub = None


def call(method, path, body=None, raw=None, headers=None):
    """(code HTTP, corps decode, en-tetes)."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method=method, headers=dict(
        {'X-Requested-With': 'reencode', 'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b'{}'), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}'), e.headers


def up():
    for _ in range(60):
        try:
            urllib.request.urlopen(URL + '/api/setup', timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def run():
    # ---------------------------------------------------------------- en memoire (sans serveur)
    db.init(os.path.join(TMP, 'unit', 'hub.db'))
    assert db.get_settings()['lease_s'] == 300
    db.set_settings({'lease_s': 900, 'quality': {'cq': 30}})
    s = db.get_settings()
    assert s['lease_s'] == 900 and s['quality']['cq'] == 30 and s['quality']['preset'] == 'p6'
    s['quality']['cq'] = 1                                  # la copie rendue n'est pas celle gardee en memoire
    assert db.get_settings()['quality']['cq'] == 30
    print('  ok  : reglages en memoire, a jour apres chaque modification')

    codes = [security.new_enroll_code(i % 2 == 0) for i in range(400)]
    assert not [c for c in codes if c[0] in '-_'], 'code commencant par un tiret'
    print('  ok  : aucun code d enrolement ne commence par un tiret')

    for bad in ('file:///etc/passwd', 'ftp://exemple/x.jpg', '/etc/passwd', ''):
        try:
            posters._get(bad)
            raise AssertionError('adresse acceptee : %r' % bad)
        except ValueError:
            pass
    print('  ok  : affiches : seules les adresses http(s) sont ouvertes')

    info = main._clean_info({'gpus': [{'id': 0, 'name': 'x' * 500, 'mem_total': '<img src=x onerror=alert(1)>',
                                       'mem_used': 12.7}, 'pas un gpu', {'name': 'sans id'}],
                             'version': ['1.5.1'], 'nvenc': 'oui', 'self_update': True, 'inconnu': {'a': 1},
                             'nvenc_error': 'e' * 1000})
    assert info['gpus'] == [{'id': '0', 'name': 'x' * 80, 'mem_total': 0, 'mem_used': 12}], info['gpus']
    assert 'nvenc' not in info and info['self_update'] is True and 'inconnu' not in info
    assert len(info['nvenc_error']) == 200 and isinstance(info['version'], str)
    assert main._clean_info('x') == {} and main._clean_info(None) == {}
    print('  ok  : etat declare par un agent ramene a des types simples')

    for bad in ("http://hub:8095'; rm -rf /", 'http://hub:8095/$(id)', 'http://hub:8095/a b', 'http://hub\n@x'):
        try:
            main._clean_settings({'public_url': bad})
            raise AssertionError('URL publique acceptee : %r' % bad)
        except main.HTTPException as e:
            assert e.status_code == 400
    assert main._clean_settings({'public_url': ' https://hub.exemple.fr:8443/reencodearr/ '})['public_url'] == \
        'https://hub.exemple.fr:8443/reencodearr'
    print('  ok  : URL publique : caracteres de commande refuses')

    journal.info('test', 'ligne 1\n[01/01 00:00:00] ERROR auth     fausse ligne\x1b[2J')
    last = db.one('SELECT message FROM logs ORDER BY id DESC LIMIT 1')['message']
    assert '\n' not in last and '\x1b' not in last
    print('  ok  : journal : une seule ligne par evenement')

    # ---------------------------------------------------------------- par HTTP
    global hub
    data = os.path.join(TMP, 'data')
    hub = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', str(PORT), '--log-level', 'warning'],
                           cwd=ROOT, env=dict(os.environ, ADMIN_PASSWORD='testpass-123', PUBLIC_URL=URL),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert up(), 'hub demarre'

    code, _, h = call('GET', '/api/setup')
    assert code == 200 and "script-src 'self'" in h['content-security-policy'] and "frame-ancestors 'none'" in \
        h['content-security-policy'] and h['cache-control'] == 'no-store' and h['x-frame-options'] == 'DENY'
    with urllib.request.urlopen(URL + '/') as r:
        assert "default-src 'self'" in r.headers['content-security-policy'] and r.headers['cache-control'] == 'no-cache'
    print('  ok  : en-tetes de securite et de cache')

    assert call('POST', '/api/login', raw=b'[1,2]')[0] == 400
    assert call('POST', '/api/login', raw=b'{pas du json')[0] == 400
    assert call('POST', '/api/login', raw=b'{"username":"' + b'a' * (3 << 20) + b'"}')[0] == 413
    assert call('POST', '/api/agent/register', raw=b'"x"')[0] == 400
    assert call('POST', '/api/login', {'username': 'admin', 'password': 'x' * 5000})[0] == 401
    assert call('POST', '/api/login', {'username': 'admin', 'password': 'testpass-123'})[0] == 200
    assert call('PUT', '/api/settings', raw=b'[]')[0] == 400
    assert call('POST', '/api/jobs/1/prioritize', raw=b'pas du json')[0] == 409       # corps facultatif : job inconnu
    print('  ok  : corps JSON invalides (400) ou trop gros (413), sans erreur interne')

    # un agent enregistre declare n'importe quoi : l'API de l'interface ne rend que des valeurs simples
    cmd = call('POST', '/api/hosts/enroll', {})[1]['command']
    enroll = cmd.split('/enroll/')[1].split()[0].strip("'\"")
    code, reg, _ = call('POST', '/api/agent/register', {'code': enroll, 'name': 'pirate\n<b>x</b>', 'info': {
        'gpus': [{'id': '0', 'name': 'GPU', 'mem_total': '"><script>alert(1)</script>', 'mem_used': {'a': 1}}]}})
    assert code == 200, reg
    bearer = {'Authorization': 'Bearer ' + reg['token']}
    assert call('POST', '/api/agent/heartbeat', {'info': {'gpus': [
        {'id': '0', 'name': 'GPU', 'mem_total': '<img src=x onerror=alert(1)>', 'mem_used': 5}], 'version': '9.9.9',
        'platform': {'x': 1}}}, headers=bearer)[0] == 200
    assert call('POST', '/api/agent/heartbeat', raw=b'[]', headers=bearer)[0] == 400
    view = next(x for x in call('GET', '/api/hosts')[1]['hosts'] if x['id'] == reg['host_id'])
    assert view['gpus'] == [{'id': '0', 'name': 'GPU', 'mem_total': 0, 'mem_used': 5}], view['gpus']
    assert '\n' not in view['name'] and isinstance(view['platform'], str)
    assert call('PATCH', '/api/hosts/%d' % reg['host_id'], {'gpus_enabled': 'tout'})[0] == 400
    print('  ok  : un agent ne peut pas injecter de contenu dans l onglet Hosts')

    # resultat demesure : refuse des l'annonce de sa taille, rien n'est ecrit dans le dossier de transit
    db.init(os.path.join(data, 'hub.db'))                   # meme base que le hub (WAL : deux processus)
    jid = db.run("INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime,host_id,gpu,lease_until) "
                 "VALUES('S/a.mkv','S',1,'encoding',?,1000,1,?,'0',?)", (time.time(), reg['host_id'], time.time() + 600))
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    c.putrequest('PUT', '/api/agent/jobs/%d/result' % jid)
    c.putheader('Authorization', bearer['Authorization'])
    c.putheader('Content-Length', str(1 << 40))
    c.endheaders()
    assert c.getresponse().status == 413
    c.close()
    assert not [f for f in os.listdir(os.path.join(data, 'staging')) if f.startswith('%d.' % jid)]
    assert db.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'] == 'encoding'
    print('  ok  : resultat demesure refuse avant ecriture')


try:
    run()
    print('TOUT EST OK')
finally:
    if hub:
        hub.kill()
    shutil.rmtree(TMP, ignore_errors=True)
