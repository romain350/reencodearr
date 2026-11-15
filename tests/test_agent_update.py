"""Bouton « Mettre a jour l'agent » de l'onglet Hosts, avec un vrai hub et trois agents (sans GPU ni ffmpeg) :
  - un agent recent (>= 1.4.0) se met a jour seul : il remplace son fichier et redemarre ;
  - un ancien agent (1.3.x) en conteneur : il s'arrete, Docker le relance, l'entrypoint telecharge le nouvel agent.py ;
  - un ancien agent en service systemd : il repart avec le meme fichier, le hub affiche la commande a lancer.
Verifie aussi qu'un encodage interrompu par la mise a jour repart en file sans tentative perdue.

Lancer depuis le dossier du projet : python tests/test_agent_update.py (environ 1 min 30)
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
PORT = 18096
URL = 'http://127.0.0.1:%d' % PORT
TMP = tempfile.mkdtemp(prefix='rh-maj-')
procs = []

# ------------------------------------------------------------------ 1. remise en file sans penalite (base seule)
os.environ.update(DATA_DIR=os.path.join(TMP, 'unit'), SERIES_DIR=TMP, MOVIES_DIR=TMP)
sys.path.insert(0, ROOT)
from app import agents, db, jobs  # noqa: E402

db.init(os.path.join(TMP, 'unit', 'hub.db'))
hid = db.run("INSERT INTO hosts(name,token_hash,enabled,gpus_enabled,info,last_seen,created) VALUES('nas','x',1,"
             "'[\"0\"]','{\"version\":\"1.4.0\",\"self_update\":true}',?,?)", (time.time(), time.time()))
db.run("INSERT INTO episodes(path,series,size,mtime,probe,codec) VALUES('S/a.mkv','S',100,1,'{\"duration\":10}','h264')")
jid = db.run("INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime) VALUES('S/a.mkv','S',1,'queued',?,100,1)",
             (time.time(),))
host = db.one('SELECT * FROM hosts WHERE id=?', (hid,))
job = jobs.claim(host, '0')
jobs.progress(jid, hid, 30, 'encode', '', job['token'])
agents.request(db.one('SELECT * FROM hosts WHERE id=?', (hid,)), 'test')
host = db.one('SELECT * FROM hosts WHERE id=?', (hid,))
assert jobs.progress(jid, hid, 31, 'encode', '', job['token']) is True, 'l encodage en cours recoit l ordre d arret'
assert agents.pending(host), 'mise a jour en attente : le hub ne donne plus de job'
jobs.fail(jid, hid, 'annule', token=job['token'])
j = db.one('SELECT * FROM jobs WHERE id=?', (jid,))
assert j['status'] == 'queued' and j['attempts'] == 0 and j['cancel'] == 0, 'remis en file sans tentative perdue'
assert agents.on_heartbeat(host, json.loads(host['info'])) == 'update', 'agent recent : ordre de mise a jour'
print('  ok  : encodage interrompu remis en file sans penalite, agent recent recoit l ordre')

# ------------------------------------------------------------------ 2. vrai hub, vrais agents
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def api(method, path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={'X-Requested-With': 'reencode',
                                                         'Content-Type': 'application/json'})
    with opener.open(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def wait(fn, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        try:
            r = fn()
        except Exception:
            r = None
        if r:
            return r
        time.sleep(1)
    return None


def host(name):
    return next((h for h in api('GET', '/api/hosts')['hosts'] if h['name'] == name), None)


def enroll(name, agent_file, mode):
    """mode : 'self' (agent lance directement), 'docker' (relance + telechargement, comme l'entrypoint),
    'systemd' (relance du meme fichier, comme Restart=always)."""
    d = os.path.join(TMP, name)
    os.makedirs(d)
    path = os.path.join(d, 'agent.py')
    shutil.copy(agent_file, path)
    code = re.search(r'/enroll/([^\' ]+)', api('POST', '/api/hosts/enroll')['command']).group(1)
    subprocess.run([sys.executable, path, 'register', '--hub', URL, '--code', code, '--name', name,
                    '--ffmpeg', 'ffmpeg', '--dir', d], check=True, capture_output=True)
    run = '%s -u %s run --dir %s' % (sys.executable, path, d)
    if mode == 'docker':     # conteneur lance avant la mise a jour du hub : tourne en 1.3.1, puis a chaque relance
        run = '%s; while true; do curl -fsS %s/agent/agent.py -o %s; %s; sleep 1; done' % (run, URL, path, run)
    elif mode == 'systemd':
        run = 'while true; do %s; sleep 1; done' % run
    log = open(os.path.join(d, 'log.txt'), 'w')
    procs.append(subprocess.Popen(['bash', '-c', 'exec ' + run if mode == 'self' else run], stdout=log,
                                  stderr=subprocess.STDOUT, start_new_session=True))
    return path


def _up():
    try:
        urllib.request.urlopen(URL + '/api/me', timeout=2)
    except urllib.error.HTTPError:
        pass                  # 401 : le hub repond
    return True


def cleanup():
    for p in procs:
        try:
            os.killpg(p.pid, 9)
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)


def main():
    env = dict(os.environ, SERIES_DIR=TMP, MOVIES_DIR=TMP, DATA_DIR=os.path.join(TMP, 'data'),
               ADMIN_PASSWORD='testpass-123', PUBLIC_URL=URL)
    procs.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', str(PORT),
                                   '--log-level', 'warning'], cwd=ROOT, env=env, start_new_session=True))
    assert wait(_up), 'hub demarre'
    api('POST', '/api/login', {'username': 'admin', 'password': 'testpass-123'})
    latest = api('GET', '/api/hosts')['agent_latest']
    print('  ok  : version servie par le hub :', latest)

    recent = os.path.join(TMP, 'recent.py')
    with open(os.path.join(ROOT, 'agent', 'agent.py')) as f:
        open(recent, 'w').write(f.read().replace("VERSION = '%s'" % latest, "VERSION = '1.3.9'"))
    old = os.path.join(TMP, 'old.py')
    with open(old, 'wb') as f:      # agent 1.3.1, avant ce changement
        f.write(subprocess.run(['git', 'show', '9ef3cb5:agent/agent.py'], cwd=ROOT, capture_output=True,
                               check=True).stdout)
    paths = {'recent': enroll('recent', recent, 'self'), 'docker': enroll('docker', old, 'docker'),
             'systemd': enroll('systemd', old, 'systemd')}
    assert wait(lambda: all((host(n) or {}).get('online') for n in paths)), 'trois agents en ligne'
    assert host('recent')['agent_version'] == '1.3.9' and host('recent')['update']['outdated']
    assert host('systemd')['agent_version'] == '1.3.1' and host('systemd')['update']['outdated']
    print('  ok  : agents en ligne, bouton propose (1.3.9 et 1.3.1 -> %s)' % latest)

    for n in ('recent', 'systemd'):
        api('POST', '/api/hosts/%d/update' % host(n)['id'])
    assert wait(lambda: host('recent')['agent_version'] == latest and not host('recent')['update']['updating']), \
        'agent recent mis a jour'
    assert "VERSION = '%s'" % latest in open(paths['recent']).read(), 'fichier de l agent remplace'
    print('  ok  : agent recent : fichier remplace, redemarre en', latest)

    assert wait(lambda: host('systemd')['update']['error']), 'agent systemd : erreur affichee'
    h = host('systemd')
    assert h['agent_version'] == '1.3.1' and 'systemctl restart' in h['update']['command'] and URL in h['update']['command']
    print('  ok  : ancien agent systemd : reste en 1.3.1, commande affichee')

    assert host('docker')['agent_version'] == '1.3.1'
    api('POST', '/api/hosts/%d/update' % host('docker')['id'])
    assert wait(lambda: host('docker')['agent_version'] == latest and not host('docker')['update']['updating']), \
        'ancien conteneur mis a jour'
    assert not host('docker')['update']['error']
    print('  ok  : ancien agent en conteneur : arrete, relance avec', latest)
    print('TOUT EST OK')


if __name__ == '__main__':
    try:
        main()
    except AssertionError as e:
        print('  ECHEC :', e)
        for n in ('recent', 'docker', 'systemd'):
            p = os.path.join(TMP, n, 'log.txt')
            if os.path.exists(p):
                print('---', n, '---\n' + open(p).read()[-1500:])
        cleanup()
        sys.exit(1)
    cleanup()
