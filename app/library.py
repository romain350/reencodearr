"""Bibliotheque : inventaire des series/episodes et des films, sondage ffprobe, scan d'integrite.

Les films vivent dans les memes tables que les series : leur chemin et leur groupe commencent par
config.MOVIE_PREFIX (voir abs_path, group_of)."""
import json
import os
import re
import subprocess
import threading
import time

from . import config, db, journal

_scan_lock = threading.Lock()
scan_queue = []                 # chemins relatifs en attente
scan_progress = {}              # serie -> [faits, total]
_scan_level = {}                # serie -> niveau


def is_movie(key):
    """Cle de groupe ou chemin relatif d'un film (prefixe config.MOVIE_PREFIX) ?"""
    return str(key).startswith(config.MOVIE_PREFIX)


def movie_key(name):
    return config.MOVIE_PREFIX + name


def display(key):
    """Nom affiche d'une serie ou d'un film (sans le prefixe interne des films)."""
    return key[len(config.MOVIE_PREFIX):] if is_movie(key) else key


def root_of(rel):
    return config.MOVIES_DIR if is_movie(rel) else config.SERIES_DIR


def abs_path(rel):
    root = os.path.normpath(root_of(rel))
    if is_movie(rel):
        rel = rel[len(config.MOVIE_PREFIX):]
    p = os.path.normpath(os.path.join(root, rel))
    if not (p == root or p.startswith(root + os.sep)):
        raise ValueError('chemin hors bibliotheque')
    return p


def rel_of(path, movie):
    """Inverse d'abs_path : chemin absolu -> chemin relatif stocke en base."""
    rel = os.path.relpath(path, config.MOVIES_DIR if movie else config.SERIES_DIR).replace(os.sep, '/')
    return config.MOVIE_PREFIX + rel if movie else rel


def group_of(rel):
    """Serie ou film auquel appartient un fichier. Un film pose directement a la racine forme son propre groupe."""
    if not is_movie(rel):
        return rel.split('/')[0]
    rest = rel[len(config.MOVIE_PREFIX):]
    return config.MOVIE_PREFIX + (rest.split('/')[0] if '/' in rest else os.path.splitext(rest)[0])


def quarantine_dest(rel, sub):
    """Emplacement en quarantaine (dossier cache a la racine de la bibliotheque concernee)."""
    plain = rel[len(config.MOVIE_PREFIX):] if is_movie(rel) else rel
    return os.path.join(root_of(rel), config.QUARANTINE, sub, plain)


def quarantine_roots():
    roots = [os.path.join(config.SERIES_DIR, config.QUARANTINE)]
    if os.path.normpath(config.MOVIES_DIR) != os.path.normpath(config.SERIES_DIR):
        roots.append(os.path.join(config.MOVIES_DIR, config.QUARANTINE))
    return roots


def _video(name):
    return not name.startswith('.') and os.path.splitext(name)[1].lower() in config.VIDEO_EXT


def _walk(top, root):
    for base, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if not _video(f):
                continue
            full = os.path.join(base, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield os.path.relpath(full, root).replace(os.sep, '/'), st.st_size, int(st.st_mtime)


def iter_files():
    """(groupe, chemin relatif, taille, mtime) pour chaque video des series puis des films."""
    root = config.SERIES_DIR
    if os.path.isdir(root):
        for s in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if s.is_dir() and not s.name.startswith('.'):
                for rel, size, mtime in _walk(s.path, root):
                    yield s.name, rel, size, mtime
    root = config.MOVIES_DIR
    if os.path.isdir(root) and os.path.normpath(root) != os.path.normpath(config.SERIES_DIR):
        for m in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if m.name.startswith('.'):
                continue
            if m.is_dir():
                for rel, size, mtime in _walk(m.path, root):
                    yield movie_key(m.name), config.MOVIE_PREFIX + rel, size, mtime
            elif m.is_file() and _video(m.name):
                st = m.stat()
                yield movie_key(os.path.splitext(m.name)[0]), config.MOVIE_PREFIX + m.name, st.st_size, int(st.st_mtime)


def refresh():
    """Synchronise la table episodes avec le disque (stat uniquement ; le sondage vient apres)."""
    seen = set()
    known = {r['path']: (r['size'], r['mtime']) for r in db.q('SELECT path,size,mtime FROM episodes')}
    for series, rel, size, mtime in iter_files():
        seen.add(rel)
        if rel not in known:
            db.run('INSERT INTO episodes(path,series,size,mtime) VALUES(?,?,?,?)', (rel, series, size, mtime))
        elif known[rel] != (size, mtime):
            db.run("UPDATE episodes SET size=?,mtime=?,probe=NULL,codec=NULL,bad=0,issues='[]' WHERE path=?",
                   (size, mtime, rel))
    for rel in set(known) - seen:
        db.run('DELETE FROM episodes WHERE path=?', (rel,))


def ffprobe(path):
    p = subprocess.run([config.FFPROBE, '-v', 'quiet', '-print_format', 'json', '-show_streams',
                        '-show_format', path], capture_output=True, text=True, timeout=300)
    return json.loads(p.stdout) if p.stdout.strip() else None


def summarize(d):
    """Resume d'un ffprobe : codec video principal (hors jaquette), pistes, langues, duree."""
    st = d['streams']
    video = [s for s in st if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audio = [s for s in st if s['codec_type'] == 'audio']
    subs = [s for s in st if s['codec_type'] == 'subtitle']
    dur = float(d.get('format', {}).get('duration') or 0)
    return {'codec': video[0]['codec_name'] if video else '',
            'width': video[0].get('width') if video else None,
            'height': video[0].get('height') if video else None,
            'duration': dur, 'video': len(video), 'audio': len(audio), 'subs': len(subs),
            'alangs': [(s.get('tags') or {}).get('language') for s in audio]}


def probe_pending(limit=40):
    rows = db.q('SELECT path,size,mtime FROM episodes WHERE probe IS NULL LIMIT ?', (limit,))
    for r in rows:
        try:
            d = ffprobe(abs_path(r['path']))
            s = summarize(d) if d else None
        except Exception:
            s = None
        if s is None:
            s = {'codec': '', 'duration': 0, 'video': 0, 'audio': 0, 'subs': 0, 'alangs': []}
            issues = [{'src': 'scan', 'msg': 'fichier illisible par ffprobe'}]
            db.run('UPDATE episodes SET probe=?,codec=?,bad=1,issues=? WHERE path=? AND size=?',
                   (json.dumps(s), '', json.dumps(issues), r['path'], r['size']))
        else:
            db.run('UPDATE episodes SET probe=?,codec=? WHERE path=? AND size=?',
                   (json.dumps(s), s['codec'], r['path'], r['size']))
    return len(rows)


# ------------------------------------------------------------------ scan d'integrite
HARMLESS = re.compile(r'non monotonically|Timestamps are unset|Past duration|deprecated', re.I)


def scan_episode(rel, level):
    """Retourne la liste des problemes detectes (vide = sain).

    quick : relecture complete du conteneur sans decoder (zones a zero, EBML casse, contenu manquant).
    full  : decode aussi la video (lent, CPU).
    """
    path = abs_path(rel)
    issues = []
    d = ffprobe(path)
    if not d:
        return ['fichier illisible par ffprobe']
    s = summarize(d)
    if not s['codec']:
        issues.append('aucune piste video')
    cmd = [config.FFMPEG, '-v', 'error', '-nostdin', '-progress', 'pipe:1', '-nostats', '-i', path]
    cmd += ['-map', '0:v:0', '-f', 'null', '-'] if level == 'full' else ['-map', '0', '-c', 'copy', '-f', 'null', '-']
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    errs = [ln for ln in p.stderr.splitlines() if ln.strip() and not HARMLESS.search(ln)]
    if errs:
        kinds = []
        for ln in errs:
            k = re.sub(r'\[[^\]]*\]\s*', '', ln)[:90]
            k = re.sub(r'0x[0-9a-f]+|pos \d+|\d+', '#', k)
            if k not in kinds:
                kinds.append(k)
        issues.append('%d erreur(s) de lecture : %s' % (len(errs), ' ; '.join(kinds[:2])))
    last = 0.0
    for ln in p.stdout.splitlines():
        if ln.startswith('out_time_us='):
            try:
                last = int(ln.split('=')[1]) / 1e6
            except ValueError:
                pass
    tol = db.get_settings()['tolerance_s']
    if s['duration'] and last and s['duration'] - last > max(tol, 1.0) and s['codec']:
        issues.append('contenu manquant (%.0f s sur %.0f s lisibles)' % (s['duration'] - last, s['duration']))
    if p.returncode != 0 and not issues:
        issues.append('lecture interrompue (code %d)' % p.returncode)
    return issues


def enqueue_scan(series_names, level):
    n = 0
    with _scan_lock:
        queued = set(scan_queue)        # liste parcourue une fois : rester rapide sur une grosse bibliotheque
        for name in series_names:
            added = 0
            for r in db.q('SELECT path FROM episodes WHERE series=?', (name,)):
                if r['path'] not in queued:
                    scan_queue.append(r['path'])
                    queued.add(r['path'])
                    added += 1
            if added:
                prev = scan_progress.get(name) or [0, 0]
                scan_progress[name] = [prev[0], prev[1] + added]
                _scan_level[name] = level
                n += added
    return n


def _scan_worker():
    while True:
        rel = None
        with _scan_lock:
            if scan_queue:
                rel = scan_queue.pop(0)
        if rel is None:
            time.sleep(2)
            continue
        row = db.one('SELECT series FROM episodes WHERE path=?', (rel,))
        series = row['series'] if row else group_of(rel)
        try:
            issues = scan_episode(rel, _scan_level.get(series, 'quick'))
        except Exception as e:
            issues = ['scan impossible : %s' % e]
        row = db.one('SELECT issues FROM episodes WHERE path=?', (rel,))
        if row:
            keep = [i for i in json.loads(row['issues']) if i.get('src') != 'scan']
            new = keep + [{'src': 'scan', 'msg': m} for m in issues]
            db.run('UPDATE episodes SET issues=?,bad=?,scanned_at=? WHERE path=?',
                   (json.dumps(new), 1 if new else 0, time.time(), rel))
            if issues:
                journal.warn('scan', '%s : %s' % (rel, ' | '.join(issues)))
        with _scan_lock:
            pr = scan_progress.get(series)
            if pr:
                pr[0] += 1
                if pr[0] >= pr[1]:
                    scan_progress.pop(series, None)


def start_background():
    n = max(1, int(db.get_settings()['scan']['concurrency']))
    for i in range(n):
        threading.Thread(target=_scan_worker, daemon=True, name='scan-%d' % i).start()

    def loop():
        while True:
            try:
                refresh()
                while probe_pending():
                    pass
            except Exception as e:
                journal.error('system', 'erreur de rafraichissement de la bibliotheque : %s' % e)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name='library').start()


def group_complete(key):
    """Serie ou film entierement traite : rien n'est en file, tous les fichiers sont analyses et sains, et chacun
    est soit dans un codec moderne (HEVC/AV1/VP9), soit un original que l'encodage a juge deja compact."""
    eps = db.q('SELECT path, codec, probe, bad FROM episodes WHERE series=?', (key,))
    if not eps:
        return False, 'aucun fichier connu'
    active = '(' + ','.join("'%s'" % a for a in config.ACTIVE) + ')'
    if db.one('SELECT 1 FROM jobs WHERE series=? AND status IN %s' % active, (key,)):
        return False, 'des encodages sont encore en file'
    left = 0
    kept = config.kept_codecs(db.get_settings())
    for e in eps:
        if e['probe'] is None:
            return False, 'des fichiers ne sont pas encore analyses'
        if e['bad']:
            return False, 'des fichiers defectueux restent'
        if (e['codec'] or '') in kept:
            continue
        last = db.one('SELECT status FROM jobs WHERE path=? ORDER BY id DESC LIMIT 1', (e['path'],))
        if not (last and last['status'] == 'skipped'):
            left += 1
    if left:
        return False, '%d fichier(s) pas encore en HEVC' % left
    return True, ''
