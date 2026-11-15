"""Installation Windows et commande reutilisable (machine non persistante), avec un vrai hub, sans GPU ni ffmpeg :
  - le hub sert le script PowerShell (commande ordinaire : usage unique ; reutilisable : relue a chaque demarrage) ;
  - une machine qui se reinstalle avec la commande reutilisable retrouve le meme host (nom, GPU coches) avec un
    nouveau jeton ; l'ancien jeton est refuse et l'encodage de l'ancienne machine repart en file sans tentative perdue ;
  - retirer le host revoque la commande.

Lancer depuis le dossier du projet : python tests/test_windows_enroll.py (quelques secondes)
"""
import http.cookiejar
import json
import os
import re
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
TMP = tempfile.mkdtemp(prefix='rh-win-')
AGENT = os.path.join(ROOT, 'agent', 'agent.py')
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def api(method, path, body=None, token=None):
    h = {'X-Requested-With': 'reencode', 'Content-Type': 'application/json'}
    if token:
        h['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=h)
    with (urllib.request.urlopen if token else opener.open)(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def status(fn):
    try:
        fn()
        return 200
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def script(code):
    with urllib.request.urlopen('%s/enroll/%s/windows' % (URL, code), timeout=10) as r:
        return r.read().decode()


def register(code, d, name='VM-WIN'):
    os.makedirs(d, exist_ok=True)
    p = subprocess.run([sys.executable, AGENT, 'register', '--hub', URL, '--code', code, '--name', name,
                        '--ffmpeg', 'ffmpeg', '--dir', d], capture_output=True, text=True)
    if p.returncode:
        return None
    return json.load(open(os.path.join(d, 'config.json')))


def main():
    env = dict(os.environ, SERIES_DIR=TMP, MOVIES_DIR=TMP, DATA_DIR=os.path.join(TMP, 'data'),
               ADMIN_PASSWORD='testpass-123', PUBLIC_URL=URL)
    hub = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', str(PORT),
                            '--log-level', 'warning'], cwd=ROOT, env=env)
    try:
        for _ in range(60):
            if status(lambda: urllib.request.urlopen(URL + '/api/me', timeout=2)) in (200, 401):
                break
            time.sleep(0.5)
        api('POST', '/api/login', {'username': 'admin', 'password': 'testpass-123'})

        # commande Linux inchangee (corps vide, comme avant)
        assert api('POST', '/api/hosts/enroll')['command'].startswith('curl -fsSL '), 'commande Linux'

        # 1. commande Windows ordinaire : usage unique
        r = api('POST', '/api/hosts/enroll', {'os': 'windows'})
        assert r['command'].startswith('powershell ') and not r['reusable'] and r['expires_in'] == 3600, r
        code = re.search(r'/enroll/([^/]+)/windows', r['command']).group(1)
        s = script(code)
        assert "$Reusable = $false" in s and "$Hub = '%s'" % URL in s and '__' not in s.replace('__DIR__', ''), s[:300]
        assert all(ord(c) < 128 for c in s), 'script en ASCII (PowerShell 5.1)'
        assert register(code, os.path.join(TMP, 'once'), name='SRV-WIN'), 'premier enregistrement'
        assert status(lambda: script(code)) == 404, 'code ordinaire consomme'
        print('  ok  : commande Windows ordinaire servie, usage unique')

        # 2. commande reutilisable : premier demarrage
        r = api('POST', '/api/hosts/enroll', {'os': 'windows', 'reusable': True})
        assert r['reusable'] and r['expires_in'] == 7 * 86400
        key = re.search(r'/enroll/([^/]+)/windows', r['command']).group(1)
        s = script(key)
        assert "$Reusable = $true" in s and "/enroll/%s/windows" % key in s, 'boot.ps1 relit le script'
        c1 = register(key, os.path.join(TMP, 'boot1'))
        h = next(x for x in api('GET', '/api/hosts')['hosts'] if x['name'] == 'VM-WIN')
        assert h['reusable_key'], 'host marque commande reutilisable'
        api('POST', '/api/agent/heartbeat', {'info': {'version': '1.5.0', 'gpus': [
            {'id': '0', 'name': 'RTX', 'mem_total': 1, 'mem_used': 0}]}}, token=c1['token'])
        api('PATCH', '/api/hosts/%d' % h['id'], {'name': 'vm-encodeur', 'gpus_enabled': ['0']})

        # un encodage en cours sur cette machine
        sys.path.insert(0, ROOT)
        os.environ.update(DATA_DIR=os.path.join(TMP, 'data'), SERIES_DIR=TMP, MOVIES_DIR=TMP)
        from app import db, jobs  # noqa: E402
        db.init(os.path.join(TMP, 'data', 'hub.db'))
        db.run("INSERT INTO episodes(path,series,size,mtime,probe,codec) VALUES('S/a.mkv','S',100,1,"
               "'{\"duration\":10}','h264')")
        jid = db.run("INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime) "
                     "VALUES('S/a.mkv','S',1,'queued',?,100,1)", (time.time(),))
        job = jobs.claim(db.one('SELECT * FROM hosts WHERE id=?', (h['id'],)), '0')
        assert job and job['id'] == jid

        # 3. la VM redemarre a zero : meme commande, meme host
        c2 = register(key, os.path.join(TMP, 'boot2'), name='WIN-ALEATOIRE')
        assert c2 and c2['token'] != c1['token'] and c2['name'] == 'vm-encodeur', c2
        hs = [x for x in api('GET', '/api/hosts')['hosts'] if x['id'] == h['id']]
        assert len(api('GET', '/api/hosts')['hosts']) == 2 and hs[0]['gpus_enabled'] == ['0'], 'meme host, GPU gardes'
        assert status(lambda: api('POST', '/api/agent/heartbeat', {'info': {}}, token=c1['token'])) == 401, \
            'ancien jeton refuse'
        assert status(lambda: api('POST', '/api/agent/heartbeat', {'info': {}}, token=c2['token'])) == 200
        j = db.one('SELECT * FROM jobs WHERE id=?', (jid,))
        assert j['status'] == 'queued' and j['attempts'] == 0 and j['host_id'] is None, dict(j)
        print('  ok  : reinstallation : meme host, nouveau jeton, encodage remis en file sans tentative perdue')

        # 4. retirer le host revoque la commande
        api('DELETE', '/api/hosts/%d' % h['id'])
        assert status(lambda: script(key)) == 404 and not register(key, os.path.join(TMP, 'boot3')), 'revoquee'
        print('  ok  : retirer le host revoque la commande reutilisable')

        # 5. une commande reutilisable jamais utilisee expire au bout de 7 jours
        r = api('POST', '/api/hosts/enroll', {'os': 'windows', 'reusable': True})
        key = re.search(r'/enroll/([^/]+)/windows', r['command']).group(1)
        from app import security  # noqa: E402
        db.run('UPDATE enroll_codes SET expires=? WHERE code_hash=?', (time.time() - 1, security.sha(key)))
        assert status(lambda: script(key)) == 404, 'jamais utilisee : expiree'
        print('  ok  : commande reutilisable jamais utilisee : expire')
        print('OK')
    finally:
        hub.kill()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == '__main__':
    main()
