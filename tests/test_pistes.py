"""Choix des pistes et signes de vie pendant l'encodage (hub 1.14.3 / agent 1.5.2) :
  - un sous-titre au codec inconnu faisait echouer ffmpeg (« Subtitle codec none (0) is not supported »,
    puis « Function not implemented ») : il est ignore, l'agent comme le controle du hub ;
  - les jaquettes MJPEG (attached_pic) devenaient des pistes video ordinaires et le hub refusait le resultat
    (« nombre de pistes video different (1 -> 3) ») : elles ne sont plus copiees ;
  - une piece jointe au codec inconnu mais avec un type MIME (polices...) reste copiee ;
  - ffmpeg qui ne publie aucune progression garde quand meme le bail, et l'annulation l'arrete ;
  - le message d'erreur garde le debut de stderr (la cause) et la fin.

La partie ffmpeg est sautee si ffmpeg/ffprobe ne sont pas installes.
Lancer depuis le dossier du projet : python tests/test_pistes.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix='rh-pistes-')
os.environ.update(DATA_DIR=os.path.join(TMP, 'data'), SERIES_DIR=os.path.join(TMP, 'series'),
                  MOVIES_DIR=os.path.join(TMP, 'films'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'agent'))
import agent  # noqa: E402
from app import library  # noqa: E402

agent.CFG.update(dir=TMP, hub='http://hub', token='t', ffmpeg=shutil.which('ffmpeg') or 'ffmpeg')
agent.log = lambda msg: None


def run(*cmd):
    subprocess.run(cmd, check=True, capture_output=True, cwd=TMP)


# ------------------------------------------------------------------ message d'erreur
long_err = 'Subtitle codec none (0) is not supported.\n' + 'x' * 1000 + '\nFunction not implemented'
short = agent.short_error(long_err)
assert len(short) <= 260 and short.startswith('Subtitle codec none') and short.endswith('Function not implemented'), short
assert agent.short_error('  court  ') == 'court'

# ------------------------------------------------------------------ signes de vie sans progression ffmpeg
agent.KeepAlive.every = 0.1
beats = []


def report(pct, phase, speed):
    beats.append(phase)
    return False


silent = [sys.executable, '-c', 'import time; time.sleep(1)']      # ne publie aucun out_time_us
rc, err, cancel = agent.run_ffmpeg({'duration': 10}, silent, report)
assert rc == 0 and not cancel and beats.count('encode') >= 5, (rc, cancel, beats)

# annulation demandee par le hub pendant ce silence : le processus est arrete aussitot
beats.clear()
t = time.time()
rc, err, cancel = agent.run_ffmpeg({'duration': 10}, [sys.executable, '-c', 'import time; time.sleep(30)'],
                                   lambda pct, phase, speed: True)
assert cancel and time.time() - t < 10, (cancel, time.time() - t)

# ------------------------------------------------------------------ vrai fichier (ffmpeg requis)
if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
    print('ok (partie ffmpeg sautee : ffmpeg/ffprobe absents)')
    sys.exit(0)

run('ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=s=320x240:d=3', '-f', 'lavfi', '-i', 'sine=d=3',
    '-c:v', 'libx264', '-c:a', 'aac', 'v.mkv')
with open(os.path.join(TMP, 'a.srt'), 'w') as f:
    f.write('1\n00:00:00,000 --> 00:00:02,000\nBonjour\n')
with open(os.path.join(TMP, 'blob.bin'), 'wb') as f:
    f.write(os.urandom(500))
run('ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=64x64:d=1', '-frames:v', '1', 'c.jpg')
# 1 video, 1 audio, 4 sous-titres SubRip, 2 jaquettes, 1 piece jointe inconnue avec type MIME
run('ffmpeg', '-y', '-v', 'error', '-i', 'v.mkv', '-i', 'a.srt', '-i', 'a.srt', '-i', 'a.srt', '-i', 'a.srt',
    '-map', '0', '-map', '1', '-map', '2', '-map', '3', '-map', '4', '-c', 'copy', '-c:s', 'srt',
    '-attach', 'c.jpg', '-attach', 'c.jpg', '-attach', 'blob.bin',
    '-metadata:s:t:0', 'mimetype=image/jpeg', '-metadata:s:t:0', 'filename=cover.jpg',
    '-metadata:s:t:1', 'mimetype=image/jpeg', '-metadata:s:t:1', 'filename=cover2.jpg',
    '-metadata:s:t:2', 'mimetype=application/x-mystere', '-metadata:s:t:2', 'filename=blob.bin', 'src0.mkv')
# les deux derniers sous-titres prennent un codec que ffmpeg ne connait pas (meme longueur : structure intacte)
data = bytearray(open(os.path.join(TMP, 'src0.mkv'), 'rb').read())
ids = [i for i in range(len(data)) if data.startswith(b'S_TEXT/UTF8', i)]
assert len(ids) == 4, ids
for i in ids[2:]:
    data[i:i + 11] = b'S_TEXT/ZZZZ'
src = os.path.join(TMP, 'src.mkv')
open(src, 'wb').write(bytes(data))

sa = library.summarize(library.ffprobe(src))
assert (sa['video'], sa['audio'], sa['subs']) == (1, 1, 2), sa

maps = agent.stream_maps(src)
assert maps == ['-map', '0:0', '-map', '0:1', '-map', '0:2', '-map', '0:3', '-map', '0:8'], maps

job = {'id': 1, 'duration': 3, 'video_kbps': 0,
       'quality': {'cpu_preset': 'ultrafast', 'cpu_crf': 30}}
out = os.path.join(TMP, 'out.mkv')
rc, err, cancel = agent.run_ffmpeg(job, agent.build_cmd(job, 'cpu', src, out, False), lambda *a: False)
assert rc == 0 and not cancel, err
sb = library.summarize(library.ffprobe(out))
assert sb['codec'] == 'hevc' and (sb['video'], sb['audio'], sb['subs']) == (1, 1, 2), sb
att = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 't', '-show_entries', 'stream=index', '-of', 'csv=p=0',
                      out], capture_output=True, text=True).stdout.split()
assert len(att) == 1, 'la piece jointe est conservee : %s' % att

# l'ancienne commande (tout copier) echoue bien sur ce fichier : le test reproduit le defaut
old = agent.build_cmd(job, 'cpu', src, out, False)
i = old.index('-i') + 2
old[i:i + len(maps)] = agent.DEFAULT_MAPS
assert subprocess.run(old, capture_output=True).returncode != 0

# ffprobe introuvable : on garde l'ancien choix plutot que d'echouer
agent.CFG['ffmpeg'] = os.path.join(TMP, 'absent', 'ffmpeg')
save = agent.ffprobe_path
agent.ffprobe_path = lambda: os.path.join(TMP, 'absent', 'ffprobe')
assert agent.stream_maps(src) == agent.DEFAULT_MAPS
agent.ffprobe_path = save
print('ok : choix des pistes, controle du hub et signes de vie pendant l encodage')
