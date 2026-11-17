"""Corrections de la revue de code (hub 1.14.1) :
  - un envoi de resultat qui se termine apres l'expiration du bail (job repris ailleurs) est refuse ;
  - un echec signale par l'agent pour un job deja recu ('verifying') ne touche ni au job ni au fichier ;
  - un signe de vie tardif ne fait pas repasser un job 'verifying' en 'uploading'.

Lancer depuis le dossier du projet : python tests/test_robustesse.py
"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix='rh-robust-')
LIB = os.path.join(TMP, 'series')
os.makedirs(os.path.join(LIB, 'S'))
os.environ.update(DATA_DIR=os.path.join(TMP, 'data'), SERIES_DIR=LIB, MOVIES_DIR=os.path.join(TMP, 'films'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, db, jobs  # noqa: E402

db.init(os.path.join(TMP, 'data', 'hub.db'))
os.makedirs(config.STAGING_DIR, exist_ok=True)
jobs._verify_q.put = lambda job_id: None          # pas de thread de verification ici
hid = db.run("INSERT INTO hosts(name,token_hash,enabled,gpus_enabled,info,last_seen,created) VALUES('gpu','x',1,"
             "'[\"0\",\"1\"]','{}',?,?)", (time.time(), time.time()))
host = db.one('SELECT * FROM hosts WHERE id=?', (hid,))
db.run("INSERT INTO episodes(path,series,size,mtime,probe,codec) VALUES('S/a.mkv','S',100,1,'{\"duration\":10}','h264')")
jid = db.run("INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime) VALUES('S/a.mkv','S',1,'queued',?,100,1)",
             (time.time(),))


def part_with(data=b'x' * 10):
    p = jobs.upload_part(jid)
    open(p, 'wb').write(data)
    return p


# 1. envoi termine apres reprise du job par un autre GPU : refuse, fichier efface, job intact
first = jobs.claim(host, '0')
db.run('UPDATE jobs SET lease_until=0 WHERE id=?', (jid,))
jobs.fail(jid, hid, 'agent injoignable (bail expire)')         # reaper
second = jobs.claim(host, '1')
p = part_with()
assert jobs.accept_upload(jid, hid, first['token'], p) is False, 'resultat de l ancienne reservation refuse'
assert not os.path.exists(p), 'fichier recu efface'
assert db.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'] == 'claimed', 'job laisse au nouveau titulaire'
assert not os.path.exists(jobs.staging_path(jid))
print('  ok  : envoi tardif d une reservation expiree refuse')

# 2. le titulaire actuel envoie : accepte
p = part_with()
assert jobs.accept_upload(jid, hid, second['token'], p) is True
assert db.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'] == 'verifying'
assert os.path.exists(jobs.staging_path(jid)) and not os.path.exists(p)
print('  ok  : envoi du titulaire accepte, job en verification')

# 3. signe de vie tardif (KeepAlive de l'envoi) : ne repasse pas en 'uploading', demande l'arret
assert jobs.progress(jid, hid, 100, 'upload', '', second['token']) is True
assert db.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'] == 'verifying'
print('  ok  : signe de vie tardif sans effet sur un job en verification')

# 4. echec signale par l'agent apres l'envoi : ignore, fichier en verification conserve
jobs.fail(jid, hid, 'reponse perdue', token=second['token'])
assert db.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'] == 'verifying'
assert os.path.exists(jobs.staging_path(jid)), 'fichier en verification conserve'
print('  ok  : echec signale apres l envoi ignore')

print('TOUT EST OK')
