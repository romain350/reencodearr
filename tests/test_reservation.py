"""Un job repris apres expiration du bail par un autre GPU de la meme machine : l'ancien worker doit s'arreter
et ne plus pouvoir agir sur le job (cas du job 3122 : deux workers dans le meme dossier, ffmpeg sans flux video).

Lancer depuis le dossier du projet : python tests/test_reservation.py
"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix='rh-resa-')
os.environ.update(DATA_DIR=TMP, SERIES_DIR=TMP, MOVIES_DIR=TMP)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, jobs  # noqa: E402

db.init(os.path.join(TMP, 'hub.db'))
hid = db.run("INSERT INTO hosts(name,token_hash,enabled,gpus_enabled,info,last_seen,created) VALUES('gpu-a','x',1,'[\"0\",\"1\"]','{}',?,?)",
             (time.time(), time.time()))
db.run("INSERT INTO episodes(path,series,size,mtime,probe,codec) VALUES('S/a.mkv','S',100,1,'{\"duration\":10}','h264')")
jid = db.run("INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime) VALUES('S/a.mkv','S',1,'queued',?,100,1)",
             (time.time(),))
host = db.one('SELECT * FROM hosts WHERE id=?', (hid,))

first = jobs.claim(host, '0')
assert first and first['token'], 'reservation avec jeton'
assert jobs.progress(jid, hid, 10, 'download', '', first['token']) is False, 'le titulaire garde le job'
db.run('UPDATE jobs SET lease_until=0 WHERE id=?', (jid,))
jobs.fail(jid, hid, 'agent injoignable (bail expire)')      # ce que fait le reaper
second = jobs.claim(host, '1')
assert second and second['id'] == jid and second['token'] != first['token'], 'repris par le GPU 1 avec un autre jeton'

assert jobs.progress(jid, hid, 20, 'download', '', first['token']) is True, 'l ancien worker recoit l ordre d arreter'
jobs.fail(jid, hid, 'ffmpeg a echoue', final=True, token=first['token'])
j = db.one('SELECT * FROM jobs WHERE id=?', (jid,))
assert j['status'] == 'claimed' and j['gpu'] == '1', 'l echec de l ancien worker est ignore'
assert jobs.progress(jid, hid, 5, 'encode', '', second['token']) is False, 'le nouveau worker continue'
assert jobs.progress(jid, hid, 6, 'encode', '') is False, 'ancien agent sans jeton : comportement inchange'
jobs.fail(jid, hid, 'vraie erreur', final=True, token=second['token'])
assert db.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'] == 'failed', 'le titulaire peut echouer'
print('ok : reservation par jeton')
