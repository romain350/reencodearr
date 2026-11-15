#!/usr/bin/env python3
"""Agent Reencodearr : tourne sur une machine Linux ou Windows avec GPU NVIDIA.

Aucune dependance hors bibliotheque standard. L'agent se connecte au hub (il n'ouvre aucun port) :
  - il envoie regulierement l'etat de ses GPU ;
  - un worker par GPU coche dans le hub demande un episode ; le hub en attribue un seul a un seul agent ;
  - il telecharge la source, encode en HEVC (NVENC), controle le resultat, le renvoie au hub.

Usage :
  agent.py register --hub URL --code CODE --name NOM --ffmpeg CHEMIN --dir DOSSIER
  agent.py run --dir DOSSIER

Mise a jour (bouton de l'onglet Hosts) : le hub repond {'update': true} au heartbeat ; l'agent ne prend plus de
job, attend la fin de ceux en cours (le hub interrompt les encodages, qui repartent en file), telecharge
agent.py depuis le hub, remplace son propre fichier et redemarre. Sous Windows, il s'arrete et la boucle run.ps1
installee par le script du hub le relance.
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = '1.5.0'
CFG = {}
STOP = threading.Event()


def log(msg):
    print(time.strftime('%d/%m %H:%M:%S'), msg, flush=True)


# ------------------------------------------------------------------ dialogue avec le hub
def headers(job=None, **extra):
    h = {'Authorization': 'Bearer ' + CFG.get('token', '')}
    if job and job.get('token'):
        h['X-Job-Token'] = job['token']     # distingue cette reservation d'une reprise du meme job par un autre GPU
    h.update(extra)
    return h


def call(method, path, body=None, timeout=30, job=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CFG['hub'] + path, data=data, method=method,
                                 headers=headers(job, **{'Content-Type': 'application/json'}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def gpu_info():
    gpus = []
    try:
        out = subprocess.run(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used',
                              '--format=csv,noheader,nounits'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15).stdout
        for line in out.strip().splitlines():
            i, name, tot, used = [x.strip() for x in line.split(',')]
            gpus.append({'id': i, 'name': name, 'mem_total': int(tot), 'mem_used': int(used)})
    except Exception:
        pass
    gpus.append({'id': 'cpu', 'name': 'Processeur (libx265, lent)', 'mem_total': 0, 'mem_used': 0})
    return gpus


def ffmpeg_has(encoder):
    try:
        out = subprocess.run([CFG['ffmpeg'], '-hide_banner', '-encoders'], capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=15).stdout
        return encoder in out
    except Exception:
        return False


_NV = {'t': 0.0, 'ok': None, 'err': ''}


def nvenc_status():
    """(ok, message) : encode vraiment une image avec hevc_nvenc. Resultat garde 10 minutes."""
    if _NV['ok'] is not None and time.time() - _NV['t'] < 600:
        return _NV['ok'], _NV['err']
    ok, err = False, ''
    if ffmpeg_has('hevc_nvenc'):
        try:
            p = subprocess.run([CFG['ffmpeg'], '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=c=black:s=256x256',
                                '-frames:v', '1', '-c:v', 'hevc_nvenc', '-f', 'null', '-'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            ok = p.returncode == 0
            err = '' if ok else (p.stderr.strip().splitlines() or ['echec'])[-1:][0][:200]
            for line in p.stderr.splitlines():
                if 'minimum required' in line or 'does not support' in line:
                    err = line.split('] ', 1)[-1][:200]
        except Exception as e:
            err = str(e)[:200]
    else:
        err = 'ffmpeg sans encodeur hevc_nvenc'
    _NV.update(t=time.time(), ok=ok, err=err)
    return ok, err


def host_info():
    ok, err = nvenc_status()
    return {'gpus': gpu_info(), 'platform': '%s %s' % (platform.system(), platform.release()),
            'version': VERSION, 'ffmpeg': CFG['ffmpeg'], 'nvenc': ok, 'nvenc_error': err, 'self_update': True}


# ------------------------------------------------------------------ encodage d'un episode
def build_cmd(job, gpu, src, out, hwdecode):
    q = job['quality']
    cmd = [CFG['ffmpeg'], '-y', '-hide_banner', '-loglevel', 'error', '-nostdin', '-progress', 'pipe:1', '-nostats']
    if gpu == 'cpu':
        enc = ['-c:v:0', 'libx265', '-preset', q['cpu_preset'], '-crf', str(q['cpu_crf']), '-x265-params', 'log-level=error']
    else:
        if hwdecode:
            cmd += ['-hwaccel', 'cuda', '-hwaccel_device', gpu, '-hwaccel_output_format', 'cuda']
        enc = ['-c:v:0', 'hevc_nvenc', '-gpu', gpu, '-preset', q['preset'], '-tune', q['tune'], '-rc', 'vbr',
               '-cq', str(q['cq']), '-b:v', '0', '-rc-lookahead', str(q['lookahead']), '-spatial-aq', '1',
               '-temporal-aq', '1', '-bf', str(q['bframes']), '-b_ref_mode', 'middle']
        # plafond de debit : evite qu'une source deja compacte soit reencodee en un fichier plus gros
        cap = int((job.get('video_kbps') or 0) * (q.get('maxrate_pct') or 0) / 100)
        if cap > 300:
            enc += ['-maxrate', '%dk' % cap, '-bufsize', '%dk' % (cap * 2)]
    # tout est copie (audio, sous-titres, pieces jointes, chapitres, autres pistes video) sauf la 1re video
    cmd += ['-i', src, '-map', '0:v', '-map', '0:a', '-map', '0:s?', '-map', '0:t?', '-map_chapters', '0',
            '-map_metadata', '0', '-c', 'copy'] + enc + ['-max_muxing_queue_size', '2048', '-f', 'matroska', out]
    return cmd


def run_ffmpeg(job, cmd, report):
    """Lance ffmpeg, remonte la progression au hub, s'arrete si le hub demande l'annulation."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                         errors='replace')
    err = []
    threading.Thread(target=lambda: err.extend(p.stderr.readlines()), daemon=True).start()
    dur = float(job.get('duration') or 0)
    last = 0
    speed = ''
    cancel = False
    for line in p.stdout:
        k, _, v = line.strip().partition('=')
        if k == 'speed':
            speed = v
        if k == 'out_time_us' and v.isdigit() and dur:
            pct = min(99.0, int(v) / 1e6 / dur * 100)
            if time.time() - last > 5:
                last = time.time()
                cancel = report(pct, 'encode', speed)
                if cancel:
                    p.kill()
                    break
        if STOP.is_set():
            p.kill()
            break
    p.wait()
    return p.returncode, ''.join(err)[-400:].strip(), cancel


def check_decode(out, gpu):
    """Decodage video complet du resultat. L'audio est copie tel quel : on ne le decode pas."""
    cmd = [CFG['ffmpeg'], '-v', 'error', '-nostdin']
    if gpu != 'cpu':
        cmd += ['-hwaccel', 'cuda', '-hwaccel_device', gpu]
    cmd += ['-i', out, '-map', '0:v:0', '-f', 'null', '-']
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if (p.returncode != 0 or p.stderr.strip()) and gpu != 'cpu':   # on retente sans acceleration
        p = subprocess.run([CFG['ffmpeg'], '-v', 'error', '-nostdin', '-i', out, '-map', '0:v:0', '-f', 'null', '-'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
    return None if p.returncode == 0 and not p.stderr.strip() else (p.stderr.strip()[:200] or 'erreur de decodage')


class KeepAlive:
    """Garde le bail du job pendant un transfert, meme si le debit s'effondre : le hub ne doit pas croire
    l'agent mort et redonner le job a un autre worker pendant que celui-ci ecrit encore."""
    every = 15          # secondes entre deux signes de vie (bail du hub : 300 s par defaut)

    def __init__(self, report, phase, pct):
        self.report, self.phase, self.pct = report, phase, pct
        self.cancel = threading.Event()
        self.stop = threading.Event()

    def __enter__(self):
        def beat():
            while not self.stop.wait(self.every):
                if self.report(self.pct(), self.phase, ''):
                    self.cancel.set()
        threading.Thread(target=beat, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


def download(job, dest, report):
    req = urllib.request.Request('%s/api/agent/jobs/%d/source' % (CFG['hub'], job['id']), headers=headers(job))
    total = job.get('size') or 0
    done = 0
    with KeepAlive(report, 'download', lambda: done / total * 100 if total else 0) as ka, \
            urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
        report(0, 'download', '')
        while True:
            if ka.cancel.is_set():
                raise RuntimeError('annule')
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
    if ka.cancel.is_set():
        raise RuntimeError('annule')
    if total and done != total:
        raise RuntimeError('telechargement incomplet (%d/%d)' % (done, total))


def upload(job, path, report):
    size = os.path.getsize(path)
    with KeepAlive(report, 'upload', lambda: 100), open(path, 'rb') as f:   # garde le bail pendant l'envoi
        req = urllib.request.Request('%s/api/agent/jobs/%d/result' % (CFG['hub'], job['id']), data=f, method='PUT',
                                     headers=headers(job, **{'Content-Type': 'application/octet-stream',
                                                             'Content-Length': str(size)}))
        with urllib.request.urlopen(req, timeout=3600) as r:
            r.read()


def process(job, gpu):
    # un dossier par reservation : si le hub redonne le job a un autre GPU de la machine, les deux workers
    # ne partagent jamais le meme fichier source
    work = os.path.join(CFG['dir'], 'work', '%d-%s' % (job['id'], job.get('token') or 'gpu' + str(gpu)))
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, 'source' + os.path.splitext(job['path'])[1])
    out = os.path.join(work, 'out.mkv')

    def report(pct, phase, speed):
        try:
            return bool(call('POST', '/api/agent/jobs/%d/progress' % job['id'],
                             {'progress': round(pct, 1), 'phase': phase, 'speed': speed}, timeout=15,
                             job=job).get('cancel'))
        except Exception:
            return False

    def fail(reason, final=False):
        log('job %d : echec : %s' % (job['id'], reason))
        try:
            call('POST', '/api/agent/jobs/%d/fail' % job['id'], {'reason': reason, 'final': final}, job=job)
        except Exception as e:
            log('impossible de prevenir le hub : %s' % e)

    try:
        free = shutil.disk_usage(work).free
        if free < (job.get('size') or 0) * 2 + (2 << 30):
            return fail('espace disque insuffisant sur %s' % CFG['dir'])
        log('job %d : %s (GPU %s)' % (job['id'], job['path'], gpu))
        download(job, src, report)
        rc, err, cancel = run_ffmpeg(job, build_cmd(job, gpu, src, out, True), report)
        if rc != 0 and not cancel and gpu != 'cpu':
            log('job %d : decodage GPU en echec (%s), nouvel essai en decodage logiciel' % (job['id'], err[:120]))
            report(0, 'encode', '')
            rc, err, cancel = run_ffmpeg(job, build_cmd(job, gpu, src, out, False), report)
        if cancel:
            return fail('annule')
        if rc != 0 or not os.path.exists(out):
            return fail('ffmpeg a echoue : %s' % (err or 'code %d' % rc))
        report(99, 'check', '')
        # le controle decode tout le film (plus de 10 min pour un 4K) : sans signe de vie, le bail expire et
        # le hub refuse ensuite l'envoi du resultat (job Resident Evil, 3125 a 3129)
        with KeepAlive(report, 'check', lambda: 99) as ka:
            bad = check_decode(out, gpu)
        if ka.cancel.is_set():
            return fail('annule')
        if bad:
            return fail('erreur au decodage du resultat : %s' % bad, final=True)
        upload(job, out, report)
        log('job %d : envoye (%.2f -> %.2f Go)' % (job['id'], os.path.getsize(src) / 1e9, os.path.getsize(out) / 1e9))
    except Exception as e:
        fail(str(e)[:250])
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ mise a jour
BUSY = set()            # GPU qui traitent un job
BUSY_LOCK = threading.Lock()


def self_update():
    """Attend la fin des jobs en cours, remplace ce fichier par l'agent.py du hub, puis redemarre."""
    try:
        while BUSY:
            STOP.wait(2)
        with urllib.request.urlopen(CFG['hub'] + '/agent/agent.py', timeout=60) as r:
            code = r.read()
        compile(code, 'agent.py', 'exec')           # refuse un fichier tronque ou invalide
        new = re.search(rb"^VERSION = '([^']+)'", code, re.M)
        if not new:
            raise RuntimeError('fichier recu sans numero de version')
        path = os.path.abspath(__file__)
        tmp = path + '.new'
        with open(tmp, 'wb') as f:
            f.write(code)
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
        log('mise a jour %s -> %s : redemarrage' % (VERSION, new.group(1).decode()))
        sys.stdout.flush()
        if os.name == 'nt':     # pas de vrai exec sous Windows : run.ps1 relance l'agent
            os._exit(0)
        os.execv(sys.executable, [sys.executable, '-u', path, 'run', '--dir', CFG['dir']])
    except Exception as e:
        log('mise a jour impossible : %s' % e)
        STATE['updating'] = False       # reprend le travail ; le hub redemandera tant que la demande tient


# ------------------------------------------------------------------ boucles
STATE = {'enabled': False, 'gpus': [], 'paused': False, 'updating': False}


def heartbeat_loop():
    while not STOP.is_set():
        try:
            r = call('POST', '/api/agent/heartbeat', {'info': host_info()})
            STATE.update(enabled=r.get('enabled', False), gpus=r.get('gpus_enabled', []), paused=r.get('paused', False))
            if r.get('update') and not STATE['updating']:
                STATE['updating'] = True
                log('mise a jour demandee par le hub')
                threading.Thread(target=self_update, daemon=True).start()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log('jeton refuse par le hub (hote supprime ?) : arret.')
                STOP.set()
        except Exception as e:
            log('hub injoignable : %s' % e)
        STOP.wait(15)


def worker_loop(gpu):
    while not STOP.is_set():
        if STATE['enabled'] and not STATE['paused'] and not STATE['updating'] and gpu in STATE['gpus'] \
                and (gpu == 'cpu' or nvenc_status()[0]):
            with BUSY_LOCK:
                BUSY.add(gpu)
            try:
                try:
                    job = call('POST', '/api/agent/claim', {'gpu': gpu}).get('job')
                except Exception:
                    job = None
                if job:
                    process(job, gpu)
            finally:
                with BUSY_LOCK:
                    BUSY.discard(gpu)
            if job:
                continue
        STOP.wait(10)


def cmd_run(a):
    CFG.update(json.load(open(os.path.join(a.dir, 'config.json'))))
    CFG['dir'] = a.dir
    log('agent %s demarre (hub %s)' % (VERSION, CFG['hub']))
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    started = set()
    while not STOP.is_set():
        for g in gpu_info():
            if g['id'] not in started:
                started.add(g['id'])
                threading.Thread(target=worker_loop, args=(g['id'],), daemon=True).start()
        STOP.wait(30)


def cmd_register(a):
    CFG.update(hub=a.hub.rstrip('/'), ffmpeg=a.ffmpeg, dir=a.dir, token='')
    body = {'code': a.code, 'name': a.name, 'info': host_info()}
    req = urllib.request.Request(CFG['hub'] + '/api/agent/register', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit('Enregistrement refuse : %s' % e.read().decode()[:200])
    os.makedirs(a.dir, exist_ok=True)
    path = os.path.join(a.dir, 'config.json')
    with open(path, 'w') as f:
        json.dump({'hub': CFG['hub'], 'token': res['token'], 'name': res['name'], 'ffmpeg': a.ffmpeg}, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print('Enregistre sous le nom %s' % res['name'])


def main():
    for stream in (sys.stdout, sys.stderr):     # un nom de fichier accentue ne doit jamais faire echouer un log
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('register')
    r.add_argument('--hub', required=True)
    r.add_argument('--code', required=True)
    r.add_argument('--name', required=True)
    r.add_argument('--ffmpeg', default='ffmpeg')
    r.add_argument('--dir', default='/opt/reencodearr-agent')
    u = sub.add_parser('run')
    u.add_argument('--dir', default='/opt/reencodearr-agent')
    a = ap.parse_args()
    try:
        cmd_register(a) if a.cmd == 'register' else cmd_run(a)
    except KeyboardInterrupt:
        STOP.set()


if __name__ == '__main__':
    main()
