"""File d'attente d'encodage : creation, reservation atomique, verification, remplacement."""
import json
import os
import queue
import secrets
import shutil
import sqlite3
import threading
import time

from . import config, db, journal, library, naming

_verify_q = queue.Queue()
ACTIVE_SQL = "('" + "','".join(config.ACTIVE) + "')"
RUNNING = ('claimed', 'encoding', 'uploading')
RUNNING_SQL = "('" + "','".join(RUNNING) + "')"


def staging_path(job_id, part=False):
    return os.path.join(config.STAGING_DIR, '%d.mkv%s' % (job_id, '.part' if part else ''))


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ creation
def _batch_for(series):
    active = db.one('SELECT 1 FROM jobs WHERE series=? AND status IN %s' % ACTIVE_SQL, (series,))
    st = db.one('SELECT batch FROM series_state WHERE series=?', (series,))
    if active and st:
        return st['batch']
    b = int(time.time() * 1000)
    db.run('INSERT INTO series_state(series,batch) VALUES(?,?) ON CONFLICT(series) DO UPDATE SET batch=excluded.batch',
           (series, b))
    return b


def enqueue(series_names, retry_bad=False):
    """Met en file tous les episodes non HEVC. L'index unique empeche tout doublon, meme concurrent."""
    rep = {'enqueued': 0, 'already_hevc': 0, 'already_modern': 0, 'skipped_compact': 0, 'skipped_bad': 0,
           'already_queued': 0, 'not_probed': 0}
    kept = config.kept_codecs(db.get_settings())
    for name in series_names:
        batch = None
        for e in db.q('SELECT path,size,mtime,codec,bad,probe,issues FROM episodes WHERE series=? ORDER BY path', (name,)):
            if e['probe'] is None:
                rep['not_probed'] += 1
                continue
            if e['codec'] in config.HEVC_CODECS:
                rep['already_hevc'] += 1
                continue
            if e['codec'] in kept:      # av1 (sauf option AV1 -> HEVC), vp9 : le HEVC les agrandirait
                rep['already_modern'] += 1
                continue
            if not retry_bad and any(i.get('src') == 'skip' for i in json.loads(e['issues'] or '[]')):
                rep['skipped_compact'] += 1             # deja essaye : l'encodage ne faisait pas gagner de place
                continue
            if not e['codec'] or (e['bad'] and not retry_bad):
                rep['skipped_bad'] += 1
                continue
            if batch is None:
                batch = _batch_for(name)
            try:
                db.run('INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime) VALUES(?,?,?,?,?,?,?)',
                       (e['path'], name, batch, 'queued', time.time(), e['size'], e['mtime']))
                rep['enqueued'] += 1
            except sqlite3.IntegrityError:
                rep['already_queued'] += 1
    return rep


def cancel_series(series_names):
    n = 0
    for name in series_names:
        n += len(db.q("SELECT 1 FROM jobs WHERE series=? AND status='queued'", (name,)))
        db.run("UPDATE jobs SET status='canceled',finished=? WHERE series=? AND status='queued'", (time.time(), name))
        db.run("UPDATE jobs SET cancel=1 WHERE series=? AND status IN %s" % RUNNING_SQL, (name,))
    return n


def cancel_job(job_id):
    db.run("UPDATE jobs SET status='canceled',finished=? WHERE id=? AND status='queued'", (time.time(), job_id))
    db.run("UPDATE jobs SET cancel=1 WHERE id=? AND status IN %s" % RUNNING_SQL, (job_id,))


def _head_batch(c):
    """Plus petit numero de lot en file : tout lot inferieur passe devant."""
    r = c.execute("SELECT MIN(batch) m FROM jobs WHERE status='queued'").fetchone()
    return (r['m'] if r and r['m'] is not None else int(time.time() * 1000))


def prioritize_job(job_id, force=False):
    """Passe un fichier en attente en tete de file.

    force : s'il n'y a aucun GPU libre, interrompt l'encodage en cours le moins avance ; celui-ci est
    remis en file juste derriere (sans compter de tentative) et le GPU libere prend le fichier force.
    Retourne {'ok', 'preempted'} ; preempted = id du job interrompu ou None.
    """
    with db.tx() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=? AND status='queued'", (job_id,)).fetchone()
        if not j:
            return {'ok': False, 'preempted': None}
        head = _head_batch(c)
        c.execute('UPDATE jobs SET batch=? WHERE id=?', (head - 2, job_id))
        if not force:
            return {'ok': True, 'preempted': None}
        now = time.time()
        slots = 0
        for h in c.execute('SELECT id,gpus_enabled,last_seen FROM hosts WHERE enabled=1').fetchall():
            if h['last_seen'] and now - h['last_seen'] < 45:
                slots += len(json.loads(h['gpus_enabled'] or '[]'))
        running = c.execute("SELECT COUNT(*) n FROM jobs WHERE status IN %s" % RUNNING_SQL).fetchone()['n']
        if slots > running:
            return {'ok': True, 'preempted': None}       # un GPU est libre : il prendra ce fichier tout de suite
        # Interrompt l'encodage le moins avance (le moins de travail perdu). L'envoi du resultat n'est pas
        # interrompu. Son lot est fixe des maintenant pour qu'il repasse juste derriere le fichier force.
        # Le controle final ('check') n'est pas interruptible : on n'y touche pas.
        v = c.execute("SELECT id FROM jobs WHERE status IN ('claimed','encoding') AND cancel=0 AND phase!='check' "
                      "ORDER BY phase='encode',progress,started DESC LIMIT 1").fetchone()
        if not v:
            return {'ok': True, 'preempted': None}
        c.execute('UPDATE jobs SET cancel=2,batch=? WHERE id=?', (head - 1, v['id']))
        return {'ok': True, 'preempted': v['id']}


def retry_job(job_id):
    j = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not j or j['status'] not in ('failed', 'canceled', 'skipped'):
        return False
    e = db.one('SELECT size,mtime FROM episodes WHERE path=?', (j['path'],))
    if not e:
        return False
    db.run("UPDATE episodes SET bad=0,issues=? WHERE path=?", (json.dumps(
        [i for i in json.loads(db.one('SELECT issues FROM episodes WHERE path=?', (j['path'],))['issues'])
         if i.get('src') not in ('encode', 'skip')]), j['path']))
    try:
        db.run('INSERT INTO jobs(path,series,batch,status,created,src_size,src_mtime) VALUES(?,?,?,?,?,?,?)',
               (j['path'], j['series'], _batch_for(j['series']), 'queued', time.time(), e['size'], e['mtime']))
    except sqlite3.IntegrityError:
        return False
    return True


# ------------------------------------------------------------------ cote agent
def claim(host, gpu):
    """Reserve atomiquement le plus ancien episode en attente pour (hote, gpu). Un seul agent peut gagner."""
    s = db.get_settings()
    if s['paused'] or not host['enabled']:
        return None
    if str(gpu) not in json.loads(host['gpus_enabled'] or '[]'):
        return None
    with db.tx() as c:
        if c.execute("SELECT 1 FROM jobs WHERE host_id=? AND gpu=? AND status IN %s" % RUNNING_SQL,
                     (host['id'], str(gpu))).fetchone():
            return None
        while True:
            j = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY batch,id LIMIT 1").fetchone()
            if not j:
                return None
            e = c.execute('SELECT size,mtime,probe,codec FROM episodes WHERE path=?', (j['path'],)).fetchone()
            if not e or (e['size'], e['mtime']) != (j['src_size'], j['src_mtime']):
                c.execute("UPDATE jobs SET status='canceled',error=?,finished=? WHERE id=?",
                          ('source modifiee ou absente, relancer', time.time(), j['id']))
                continue
            now = time.time()
            token = secrets.token_hex(8)
            c.execute("UPDATE jobs SET status='claimed',host_id=?,gpu=?,started=?,lease_until=?,attempts=attempts+1,"
                      "progress=0,phase='',speed='',error='',claim_token=? WHERE id=?",
                      (host['id'], str(gpu), now, now + s['lease_s'], token, j['id']))
            probe = json.loads(e['probe'] or '{}')
            dur = probe.get('duration') or 0
            kbps = int(j['src_size'] * 8 / dur / 1000 * 0.9) if dur else 0     # debit video approximatif de la source
            if e['codec'] in config.AV1_CODECS:
                kbps = 0    # AV1 -> HEVC : pas de plafond de debit, le fichier peut grossir (limite verifiee au retour)
            journal.info('queue', 'job %d reserve par %s (GPU %s) : %s' % (j['id'], host['name'], gpu, j['path']))
            return {'id': j['id'], 'path': j['path'], 'series': j['series'], 'size': j['src_size'],
                    'duration': probe.get('duration', 0), 'quality': s['quality'], 'video_kbps': kbps, 'token': token}


def owns(j, host_id, token):
    """Le job appartient-il encore a ce worker ? Le jeton distingue deux reservations du meme job par le meme
    hote (bail expire puis job repris par un autre GPU de la machine). Sans jeton (ancien agent) : hote seul."""
    if not j or (host_id is not None and j['host_id'] != host_id):
        return False
    return not token or not j['claim_token'] or token == j['claim_token']


def progress(job_id, host_id, pct, phase, speed, token=None):
    """Heartbeat d'un job : renouvelle le bail. Retourne True si l'annulation est demandee
    (ou si le job ne lui appartient plus : le worker doit alors s'arreter)."""
    with db.tx() as c:      # lecture et ecriture ensemble : un envoi qui se termine ne doit pas etre ecrase
        j = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not owns(j, host_id, token) or j['status'] not in RUNNING:
            return True
        status = 'uploading' if phase == 'upload' else 'encoding'
        labels = {'download': 'telechargement de la source', 'encode': 'encodage en cours',
                  'check': 'controle du decodage', 'upload': 'envoi du resultat'}
        if j['phase'] != phase and phase in labels:
            journal.info('queue', 'job %d : %s (%s)' % (job_id, labels[phase], j['path'].rsplit('/', 1)[-1]))
        c.execute('UPDATE jobs SET status=?,progress=?,phase=?,speed=?,lease_until=? WHERE id=?',
                  (status, pct, phase, speed, time.time() + db.get_settings()['lease_s'], job_id))
        return bool(j['cancel'])


def _record_permanent_failure(j, reason):
    row = db.one('SELECT issues FROM episodes WHERE path=?', (j['path'],))
    if row:
        issues = [i for i in json.loads(row['issues']) if i.get('src') != 'encode']
        issues.append({'src': 'encode', 'msg': reason})
        db.run('UPDATE episodes SET bad=1,issues=? WHERE path=?', (json.dumps(issues), j['path']))


def fail(job_id, host_id, reason, final=False, token=None):
    """Echec cote agent : remise en file tant qu'il reste des tentatives, sinon echec definitif."""
    with db.tx():       # le reaper et l'agent peuvent signaler le meme job en meme temps
        _fail(job_id, host_id, reason, final, token)


def _fail(job_id, host_id, reason, final, token):
    j = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    # Un job deja recu ('verifying') appartient au hub : un echec signale par l'agent (reponse a l'envoi perdue,
    # par exemple) ne doit ni effacer le fichier en cours de verification ni remettre le job en file.
    if not owns(j, host_id, token) or j['status'] not in RUNNING:
        return
    if j['cancel'] == 2:     # interrompu pour laisser passer un fichier force : retour en tete, sans penalite
        journal.info('queue', 'job %d interrompu pour un fichier force, remis en file (%s)' % (
            job_id, j['path'].rsplit('/', 1)[-1]))
        db.run("UPDATE jobs SET status='queued',host_id=NULL,gpu=NULL,progress=0,phase='',speed='',cancel=0,"
               "attempts=MAX(attempts-1,0),error='interrompu pour un fichier force' WHERE id=?", (job_id,))
    elif j['cancel'] == 3:   # interrompu pour la mise a jour de l'agent : remis en file, sans penalite
        journal.info('queue', 'job %d interrompu pour la mise a jour de l agent, remis en file (%s)' % (
            job_id, j['path'].rsplit('/', 1)[-1]))
        db.run("UPDATE jobs SET status='queued',host_id=NULL,gpu=NULL,progress=0,phase='',speed='',cancel=0,"
               "attempts=MAX(attempts-1,0),error='interrompu pour la mise a jour de l agent' WHERE id=?", (job_id,))
    elif j['cancel'] == 4:   # la machine s'est reinstallee (VM non persistante redemarree) : sans penalite
        journal.info('queue', 'job %d repris : sa machine a redemarre, remis en file (%s)' % (
            job_id, j['path'].rsplit('/', 1)[-1]))
        db.run("UPDATE jobs SET status='queued',host_id=NULL,gpu=NULL,progress=0,phase='',speed='',cancel=0,"
               "attempts=MAX(attempts-1,0),error='machine redemarree' WHERE id=?", (job_id,))
    elif j['cancel']:
        journal.info('queue', 'job %d annule (%s)' % (job_id, j['path'].rsplit('/', 1)[-1]))
        db.run("UPDATE jobs SET status='canceled',finished=?,error='annule' WHERE id=?", (time.time(), job_id))
    elif not final and j['attempts'] < db.get_settings()['max_attempts']:
        journal.warn('queue', 'job %d (%s) : %s -> remis en file (tentative %d/%d)' % (
            job_id, j['path'].rsplit('/', 1)[-1], reason, j['attempts'], db.get_settings()['max_attempts']))
        db.run("UPDATE jobs SET status='queued',host_id=NULL,gpu=NULL,error=?,progress=0,phase='' WHERE id=?",
               ('tentative %d : %s' % (j['attempts'], reason), job_id))
    else:
        journal.error('queue', 'job %d (%s) ECHEC DEFINITIF : %s' % (job_id, j['path'].rsplit('/', 1)[-1], reason))
        db.run("UPDATE jobs SET status='failed',error=?,finished=? WHERE id=?", (reason, time.time(), job_id))
        _record_permanent_failure(j, reason)


def reaper_loop():
    while True:
        try:
            for j in db.q("SELECT id,host_id FROM jobs WHERE status IN %s AND lease_until < ?" % RUNNING_SQL,
                          (time.time(),)):
                fail(j['id'], j['host_id'], 'agent injoignable (bail expire)')
        except Exception as e:
            print('[reaper]', e, flush=True)
        time.sleep(20)


# ------------------------------------------------------------------ verification et remplacement
def upload_part(job_id):
    """Fichier temporaire propre a un envoi : deux envois du meme job (bail expire puis job repris) n'ecrivent
    jamais dans le meme fichier."""
    return os.path.join(config.STAGING_DIR, '%d.%s.mkv.part' % (job_id, secrets.token_hex(6)))


def accept_upload(job_id, host_id, token, part):
    """Fin d'un envoi : si le job appartient toujours a cet agent, le fichier recu devient le resultat a verifier.
    Le controle et le passage en 'verifying' se font d'un bloc : pendant un long envoi, le bail a pu expirer et le
    job etre repris ailleurs. Retourne False si le resultat est refuse (le fichier recu est alors efface)."""
    with db.tx() as c:
        j = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not owns(j, host_id, token) or j['status'] not in RUNNING:
            _rm(part)
            return False
        os.replace(part, staging_path(job_id))
        c.execute("UPDATE jobs SET status='verifying',progress=100,phase='verification',out_size=? WHERE id=?",
                  (os.path.getsize(staging_path(job_id)), job_id))
    _verify_q.put(job_id)
    return True


def verify_loop():
    while True:
        job_id = _verify_q.get()
        try:
            finalize(job_id)
        except Exception as e:
            _fail_final(job_id, 'erreur interne a la verification : %s' % e)
        finally:
            _rm(staging_path(job_id))
            _rm(staging_path(job_id, True))


def _fail_final(job_id, reason):
    j = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    if j and j['status'] == 'verifying':
        journal.error('replace', 'job %d (%s) refuse a la verification : %s -> l original est conserve' % (
            job_id, j['path'].rsplit('/', 1)[-1], reason))
        db.run("UPDATE jobs SET status='failed',error=?,finished=? WHERE id=?", (reason, time.time(), job_id))
        _record_permanent_failure(j, reason)


def _skip(job_id, j, reason):
    """Resultat valide mais sans interet : ce n'est ni une erreur ni un episode defectueux."""
    journal.warn('replace', 'job %d (%s) ignore : %s' % (job_id, j['path'].rsplit('/', 1)[-1], reason))
    db.run("UPDATE jobs SET status='skipped',error=?,finished=? WHERE id=?", (reason, time.time(), job_id))
    row = db.one('SELECT issues FROM episodes WHERE path=?', (j['path'],))
    if row:
        issues = [i for i in json.loads(row['issues']) if i.get('src') != 'skip']
        issues.append({'src': 'skip', 'msg': reason})
        db.run('UPDATE episodes SET issues=? WHERE path=?', (json.dumps(issues), j['path']))
    _series_hook(j['series'])


def _series_hook(series):
    """Hors du chemin critique : l'appel a Sonarr / Radarr ne doit jamais ralentir ni faire echouer un encodage."""
    from . import radarr, sonarr
    if library.is_movie(series):
        threading.Thread(target=radarr.on_movie_progress, args=(series,), daemon=True, name='radarr-fin').start()
    else:
        threading.Thread(target=sonarr.on_series_progress, args=(series,), daemon=True, name='sonarr-fin').start()


def requeue_pending_verifications():
    """Au demarrage : un job reste en 'verifying' si le serveur s'est arrete pendant la verification."""
    for j in db.q("SELECT id FROM jobs WHERE status='verifying'"):
        if os.path.exists(staging_path(j['id'])):
            _verify_q.put(j['id'])
        else:
            _fail_final(j['id'], 'fichier encode perdu (redemarrage du serveur)')


def finalize(job_id):
    j = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    s = db.get_settings()
    staged = staging_path(job_id)
    src = library.abs_path(j['path'])
    try:
        st = os.stat(src)
    except OSError:
        return _fail_final(job_id, 'original introuvable')
    if st.st_size != j['src_size'] or int(st.st_mtime) != j['src_mtime']:
        return _fail_final(job_id, 'original modifie pendant l encodage')
    a, b = library.ffprobe(src), library.ffprobe(staged)
    if not a or not b:
        return _fail_final(job_id, 'fichier illisible par ffprobe')
    sa, sb = library.summarize(a), library.summarize(b)
    if abs(sa['duration'] - sb['duration']) > s['tolerance_s']:
        return _fail_final(job_id, 'duree differente (%.1f s -> %.1f s)' % (sa['duration'], sb['duration']))
    for k, label in (('video', 'video'), ('audio', 'audio'), ('subs', 'sous-titres')):
        if sa[k] != sb[k]:
            return _fail_final(job_id, 'nombre de pistes %s different (%d -> %d)' % (label, sa[k], sb[k]))
    if sa['alangs'] != sb['alangs']:
        return _fail_final(job_id, 'langues audio differentes')
    if sb['codec'] not in config.HEVC_CODECS:
        return _fail_final(job_id, 'la video n est pas en HEVC')
    out_size = os.path.getsize(staged)
    gain = 100.0 * (1 - out_size / st.st_size)
    if sa['codec'] in config.AV1_CODECS:
        # AV1 -> HEVC : on change de codec pour la compatibilite, un fichier plus gros est accepte jusqu'a la limite
        growth = s['av1']['max_growth_pct']
        if -gain > growth:
            return _skip(job_id, j, 'AV1 -> HEVC : le fichier grossirait de %.0f %% (limite %d %%), l original est conserve' % (
                -gain, growth))
    elif gain < s['min_gain_pct']:
        return _skip(job_id, j, 'deja compact : l encodage %s, l original est conserve' % (
            'grossirait le fichier de %.0f %%' % -gain if gain < 0 else 'ne ferait gagner que %.0f %%' % gain))

    target_name = naming.new_name(os.path.basename(src), s['rename'])
    target = os.path.join(os.path.dirname(src), target_name)
    if os.path.exists(target) and os.path.normpath(target) != os.path.normpath(src):
        return _fail_final(job_id, 'un fichier du meme nom existe deja : ' + target_name)
    replace_file(src, staged, target, s['replace_mode'], library.quarantine_dest(j['path'], 'originaux'))

    new_rel = library.rel_of(target, library.is_movie(j['path']))
    group = library.group_of(new_rel)    # identique pour une serie ; suit le nouveau nom d'un film pose a la racine
    nst = os.stat(target)
    sb['codec'] = 'hevc'
    db.run('DELETE FROM episodes WHERE path IN (?,?)', (j['path'], new_rel))
    db.run('INSERT INTO episodes(path,series,size,mtime,probe,codec) VALUES(?,?,?,?,?,?)',
           (new_rel, group, nst.st_size, int(nst.st_mtime), json.dumps(sb), 'hevc'))
    db.run("UPDATE jobs SET status='done',finished=?,error='',out_size=? WHERE id=?",
           (time.time(), nst.st_size, job_id))
    journal.info('replace', 'job %d OK : %s -> %s (%.2f -> %.2f Go, %+.0f %%), original %s' % (
        job_id, os.path.basename(j['path']), target_name, st.st_size / 1e9, nst.st_size / 1e9, -gain,
        'conserve en quarantaine' if s['replace_mode'] == 'quarantine' else 'supprime'))
    _series_hook(group)


def replace_file(src, staged, target, mode, quarantine_to):
    """Copie sous un nom cache (.part, ignore par Jellyfin), controle la taille, renomme atomiquement,
    et seulement apres traite l'original (suppression ou quarantaine)."""
    tmp = os.path.join(os.path.dirname(target), '.' + os.path.basename(target) + '.part')
    try:
        shutil.copyfile(staged, tmp)
        if os.path.getsize(tmp) != os.path.getsize(staged):
            raise IOError('copie incomplete')
        st = os.stat(src)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
        except (OSError, AttributeError):
            pass
    except BaseException:
        _rm(tmp)
        raise
    same = os.path.normpath(src) == os.path.normpath(target)
    if mode == 'quarantine':
        dest = quarantine_to
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        os.replace(tmp, target)
    elif same:
        os.replace(tmp, target)
    else:
        os.replace(tmp, target)
        os.remove(src)


# ------------------------------------------------------------------ episodes (et films) defectueux
def remove_bad(series_names, mode):
    """Retire les episodes marques mauvais (quarantaine ou suppression). Retourne [(chemin)]."""
    removed = []
    for name in series_names:
        for e in db.q('SELECT path FROM episodes WHERE series=? AND bad=1', (name,)):
            if db.one('SELECT 1 FROM jobs WHERE path=? AND status IN %s' % ACTIVE_SQL, (e['path'],)):
                continue
            src = library.abs_path(e['path'])
            if os.path.exists(src):
                if mode == 'delete':
                    os.remove(src)
                else:
                    dest = library.quarantine_dest(e['path'], 'mauvais')
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(src, dest)
            db.run('DELETE FROM episodes WHERE path=?', (e['path'],))
            removed.append(e['path'])
    return removed


def quarantine_info():
    n = size = 0
    for root in library.quarantine_roots():
        for base, _, files in os.walk(root):
            for f in files:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
    return {'files': n, 'size': size}


def purge_quarantine():
    info = quarantine_info()
    for root in library.quarantine_roots():
        shutil.rmtree(root, ignore_errors=True)
    return info


def start_background():
    os.makedirs(config.STAGING_DIR, exist_ok=True)
    for f in os.listdir(config.STAGING_DIR):
        if f.endswith('.part'):
            _rm(os.path.join(config.STAGING_DIR, f))
    requeue_pending_verifications()
    threading.Thread(target=verify_loop, daemon=True, name='verify').start()
    threading.Thread(target=reaper_loop, daemon=True, name='reaper').start()
