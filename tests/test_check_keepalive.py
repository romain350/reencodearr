"""Controle du decodage plus long que le bail du hub (job Resident Evil 4K, 3125 a 3129) : l'agent doit envoyer
des signes de vie pendant tout le controle, sinon le hub declare le job perdu et refuse ensuite le resultat.

Lancer depuis le dossier du projet : python tests/test_check_keepalive.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent'))
import agent  # noqa: E402

TMP = tempfile.mkdtemp(prefix='rh-check-')
agent.CFG.update(dir=TMP, hub='http://hub', token='t', ffmpeg='ffmpeg')
agent.KeepAlive.every = 0.1
calls = []


def fake_call(method, path, body=None, timeout=30, job=None):
    calls.append((path.rsplit('/', 1)[-1], (body or {}).get('phase'), time.time()))
    return {'cancel': False}


def fake_ffmpeg(job, cmd, report):
    open(cmd[-1], 'wb').write(b'x')
    return 0, '', False


def slow_check(out, gpu):
    time.sleep(1)           # bien plus long que l'intervalle des signes de vie
    return None


uploaded = []
agent.call = fake_call
agent.download = lambda job, dest, report: open(dest, 'wb').write(b'x')
agent.run_ffmpeg = fake_ffmpeg
agent.check_decode = slow_check
agent.upload = lambda job, path, report: uploaded.append(path)

job = {'id': 1, 'token': 'abc', 'path': 'Film/film.mkv', 'size': 1, 'duration': 10, 'quality': {}}
agent.build_cmd = lambda job, gpu, src, out, hw: ['ffmpeg', out]
agent.process(job, '0')
checks = [t for name, phase, t in calls if name == 'progress' and phase == 'check']
assert len(checks) >= 5, 'signes de vie pendant le controle : %d' % len(checks)
assert uploaded, 'le resultat est envoye'
assert not [c for c in calls if c[0] == 'fail'], 'aucun echec'

# annulation demandee par l'utilisateur pendant le controle : le job est abandonne
calls.clear()
uploaded.clear()
agent.call = lambda method, path, body=None, timeout=30, job=None: (
    calls.append((path.rsplit('/', 1)[-1], (body or {}).get('phase'), 0)) or {'cancel': (body or {}).get('phase') == 'check'})
agent.process(job, '0')
assert not uploaded and [c for c in calls if c[0] == 'fail'], 'annule pendant le controle'
print('ok : signes de vie pendant le controle du decodage')
