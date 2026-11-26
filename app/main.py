"""Reencodearr : API, interface web et point d'entree des agents."""
import json
import os
import re
import shlex
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import agents, backup, config, db, jobs, journal, library, posters, radarr, security, sonarr

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_FILE = os.path.join(os.path.dirname(HERE), 'agent', 'agent.py')
FFMPEG_URL = os.environ.get(
    'AGENT_FFMPEG_URL',
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz')
FFMPEG_URL_WINDOWS = os.environ.get(
    'AGENT_FFMPEG_URL_WINDOWS',
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip')
# Python embarque (zip, sans installation) pour les agents Windows
PYTHON_URL_WINDOWS = os.environ.get(
    'AGENT_PYTHON_URL_WINDOWS', 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip')
COOKIE = 'rh_session'


@asynccontextmanager
async def lifespan(app):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.STAGING_DIR, exist_ok=True)
    os.makedirs(config.POSTER_DIR, exist_ok=True)
    db.init(os.path.join(config.DATA_DIR, 'hub.db'))
    # Installation existante (compte deja cree avant l'assistant) : on ne le propose pas.
    if security.has_users() and not db.has_setting('setup_done'):
        db.set_settings({'setup_done': True})
    if security.ensure_admin(config.ADMIN_USER, config.ADMIN_PASSWORD):
        journal.info('auth', 'compte %s cree depuis ADMIN_PASSWORD' % config.ADMIN_USER)
    if not security.has_users():
        print('=' * 60, "\n Premier lancement : ouvrez l'interface web pour creer le compte\n"
              " administrateur et configurer Sonarr, Radarr et TMDB.\n" + '=' * 60, flush=True)
    journal.info('system', 'Reencodearr %s demarre (series %s, films %s%s)' % (
        config.VERSION, config.SERIES_DIR, config.MOVIES_DIR, '' if os.path.isdir(config.MOVIES_DIR) else ' : dossier absent'))
    # migration : d'anciens refus pour gain insuffisant avaient marque a tort des episodes comme defectueux
    for r in db.q("SELECT path,issues FROM episodes WHERE issues LIKE '%gain insuffisant%'"):
        keep = [i for i in json.loads(r['issues']) if 'gain insuffisant' not in i.get('msg', '')]
        db.run('UPDATE episodes SET issues=?,bad=? WHERE path=?',
               (json.dumps(keep), 1 if any(i.get('src') in ('scan', 'encode') for i in keep) else 0, r['path']))
    jobs.start_background()
    library.start_background()
    yield


app = FastAPI(title='Reencodearr', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# Politique de contenu : l'interface ne charge que ses propres fichiers. Un texte venu d'ailleurs (nom de fichier,
# reponse d'un agent) qui echapperait a l'echappement ne peut ni executer de script ni envoyer de donnees dehors.
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
_SECURITY_HEADERS = ((b'x-frame-options', b'DENY'), (b'x-content-type-options', b'nosniff'),
                     (b'referrer-policy', b'same-origin'), (b'content-security-policy', _CSP.encode()))
_NO_STORE = (b'cache-control', b'no-store')     # reponses de l'API (reglages, export avec cles...) : jamais en cache


class SecurityHeaders:
    """L'interface ne doit pas pouvoir etre affichee dans le cadre d'un autre site (clics pieges) ni ses reponses
    interpretees avec un autre type que celui annonce. Middleware ASGI pur : les envois des agents (plusieurs Go)
    passent sans etre mis en memoire."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.inner(scope, receive, send)

        extra = _SECURITY_HEADERS + ((_NO_STORE,) if scope['path'].startswith('/api/') else ())

        async def send_with_headers(msg):
            if msg['type'] == 'http.response.start':
                have = {k.lower() for k, _ in msg.get('headers', [])}
                msg = dict(msg, headers=list(msg.get('headers', [])) + [h for h in extra if h[0] not in have])
            await send(msg)
        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


# ------------------------------------------------------------------ dependances
def ui(request: Request):
    if request.method not in ('GET', 'HEAD') and request.headers.get('x-requested-with') != 'reencode':
        raise HTTPException(403, 'en-tete manquant')
    user = security.session_user(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, 'non connecte')
    return user


def agent(request: Request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        raise HTTPException(401)
    row = db.one('SELECT * FROM hosts WHERE token_hash=?', (security.sha(auth[7:]),))
    if not row:
        raise HTTPException(401)
    db.run('UPDATE hosts SET last_seen=? WHERE id=?', (time.time(), row['id']))
    return row


MAX_JSON = 2 << 20      # aucun corps JSON legitime (reglages, fichier de configuration importe) n'approche 2 Mo


async def _json(request, strict=True):
    """Corps JSON d'une requete : toujours un objet, de taille bornee (les routes ouvertes sans connexion ne doivent
    pas pouvoir remplir la memoire du hub). strict=False : un corps absent ou invalide vaut {}."""
    size = request.headers.get('content-length')
    if size and size.isdigit() and int(size) > MAX_JSON:
        raise HTTPException(413, 'corps trop volumineux')
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_JSON:
            raise HTTPException(413, 'corps trop volumineux')
    try:
        b = json.loads(bytes(raw))
    except ValueError:
        b = None
    if not isinstance(b, dict):
        if strict:
            raise HTTPException(400, 'corps invalide')
        return {}
    return b


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _clean_info(info):
    """Etat envoye par un agent (GPU, version, ffmpeg) : types et longueurs imposes avant de l'enregistrer et de
    l'afficher. Un agent n'est pas l'administrateur : ce qu'il envoie ne doit pas pouvoir toucher l'interface."""
    if not isinstance(info, dict):
        return {}
    out = {}
    if isinstance(info.get('gpus'), list):
        out['gpus'] = [{'id': str(g['id'])[:16], 'name': str(g.get('name') or '')[:80],
                        'mem_total': _int(g.get('mem_total')), 'mem_used': _int(g.get('mem_used'))}
                       for g in info['gpus'][:64] if isinstance(g, dict) and g.get('id') is not None]
    for k, n in (('platform', 80), ('version', 20), ('ffmpeg', 260), ('nvenc_error', 200)):
        if info.get(k) is not None:
            out[k] = str(info[k])[:n]
    for k in ('nvenc', 'self_update'):
        if isinstance(info.get(k), bool):
            out[k] = info[k]
    return out


def base_url(request):
    return (db.get_settings()['public_url'] or config.PUBLIC_URL or str(request.base_url)).rstrip('/')


# ------------------------------------------------------------------ authentification
@app.post('/api/login')
async def login(request: Request, response: Response):
    if request.headers.get('x-requested-with') != 'reencode':
        raise HTTPException(403)
    body = await _json(request)
    ip = request.client.host if request.client else '?'
    # scrypt occupe le processeur un instant : hors de la boucle d'evenements, pour ne pas figer agents et interface
    token, err = await run_in_threadpool(security.login, str(body.get('username', ''))[:200],
                                         str(body.get('password', '')), ip)
    if err == 'trop':
        journal.warn('auth', 'connexion bloquee (trop d echecs) depuis %s' % ip)
        raise HTTPException(429, 'trop de tentatives, reessayez dans quelques minutes')
    if err:
        journal.warn('auth', 'echec de connexion pour "%s" depuis %s' % (str(body.get('username', ''))[:40], ip))
        raise HTTPException(401, 'identifiants incorrects')
    journal.info('auth', 'connexion de %s depuis %s' % (body.get('username'), ip))
    _set_session_cookie(request, response, token)
    return {'ok': True}


def _set_session_cookie(request, response, token):
    secure = request.headers.get('x-forwarded-proto', request.url.scheme) == 'https'
    response.set_cookie(COOKIE, token, httponly=True, samesite='strict', secure=secure,
                        max_age=security.SESSION_TTL, path='/')


# ------------------------------------------------------------------ assistant de premier lancement
@app.get('/api/setup')
def setup_state():
    """Public : indique a l'interface s'il faut afficher l'assistant (et s'il faut creer le compte admin)."""
    admin_needed = not security.has_users()
    return {'admin_needed': admin_needed, 'done': not admin_needed and db.get_settings()['setup_done'],
            'version': config.VERSION}


@app.post('/api/setup/admin')
async def setup_admin(request: Request, response: Response):
    """Public, mais seulement tant qu'aucun compte n'existe : cree l'admin puis ouvre sa session."""
    if request.headers.get('x-requested-with') != 'reencode':
        raise HTTPException(403)
    b = await _json(request)
    user, pw = str(b.get('username', '')).strip(), str(b.get('password', ''))
    err = await run_in_threadpool(security.create_first_admin, user, pw)
    if err:
        raise HTTPException(409 if 'existe' in err else 400, err)
    ip = request.client.host if request.client else '?'
    journal.info('auth', 'assistant : compte administrateur %s cree depuis %s' % (user, ip))
    _set_session_cookie(request, response, security.open_session(user))
    return {'ok': True}


@app.post('/api/setup/finish')
def setup_finish(user=Depends(ui)):
    db.set_settings({'setup_done': True})
    journal.info('system', '%s termine l assistant de premier lancement' % user)
    return {'ok': True}


@app.post('/api/setup/restart')
def setup_restart(user=Depends(ui)):
    db.set_settings({'setup_done': False})
    journal.info('system', '%s relance l assistant de premier lancement' % user)
    return {'ok': True}


@app.post('/api/logout')
def logout(request: Request, response: Response, user=Depends(ui)):
    security.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


@app.get('/api/me')
def me(user=Depends(ui)):
    return {'user': user, 'version': config.VERSION}


@app.post('/api/password')
async def password(request: Request, user=Depends(ui)):
    b = await _json(request)
    journal.info('auth', '%s change son mot de passe' % user)
    if not await run_in_threadpool(security.change_password, user, str(b.get('old', '')), str(b.get('new', ''))):
        raise HTTPException(400, 'ancien mot de passe incorrect ou nouveau trop court (8 caracteres minimum)')
    return {'ok': True}


# ------------------------------------------------------------------ series et films
MOVIE_LIKE = config.MOVIE_PREFIX + '%'


def _kept_sql():
    """Liste SQL des codecs laisses tels quels (constantes internes, pas de saisie utilisateur)."""
    return "(" + ",".join("'%s'" % c for c in sorted(config.kept_codecs(db.get_settings()))) + ")"


def _group_rows(movies=False):
    """Une ligne par serie (ou par film) : comptages, taille, etat du dernier lot d'encodage, scan en cours."""
    where = 'WHERE series %s LIKE ?' % ('' if movies else 'NOT')
    kept = _kept_sql()
    rows = db.q("SELECT series, COUNT(*) n, SUM(size) size, SUM(codec IN ('hevc','h265')) hevc, "
                "SUM(probe IS NULL) pending, SUM(bad) bad, "
                "SUM(CASE WHEN probe IS NOT NULL AND codec NOT IN %s THEN size ELSE 0 END) todo_size, "
                "SUM(CASE WHEN probe IS NOT NULL AND codec NOT IN %s THEN 1 ELSE 0 END) todo "
                "FROM episodes %s GROUP BY series" % (kept, kept, where), (MOVIE_LIKE,))
    jobs_by = {}
    for r in db.q("SELECT j.series, j.status, COUNT(*) c FROM jobs j JOIN series_state s "
                  "ON s.series=j.series AND s.batch=j.batch GROUP BY j.series, j.status"):
        jobs_by.setdefault(r['series'], {})[r['status']] = r['c']
    main_file = {}
    if movies:      # le plus gros fichier d'un film est le film lui-meme (les autres : bonus, extraits...)
        # MAX(size) : SQLite donne alors les autres colonnes de cette meme ligne ; un seul sondage a relire par film
        for e in db.q('SELECT series, codec, probe, MAX(size) FROM episodes %s GROUP BY series' % where, (MOVIE_LIKE,)):
            p = json.loads(e['probe']) if e['probe'] else {}
            main_file[e['series']] = {'codec': e['codec'] or '', 'height': p.get('height'), 'duration': p.get('duration')}
    out = []
    with library._scan_lock:
        scans = {k: list(v) for k, v in library.scan_progress.items()}
    for r in rows:
        js = jobs_by.get(r['series'], {})
        total = sum(v for k, v in js.items() if k != 'canceled')
        sc = scans.get(r['series'])
        row = {'name': library.display(r['series']), 'episodes': r['n'], 'size': r['size'] or 0, 'hevc': r['hevc'] or 0,
               'pending_probe': r['pending'] or 0, 'bad': r['bad'] or 0,
               'todo': r['todo'] or 0, 'todo_size': r['todo_size'] or 0,
               'batch': {'total': total, 'done': js.get('done', 0) + js.get('skipped', 0), 'failed': js.get('failed', 0),
                         'active': sum(js.get(k, 0) for k in config.ACTIVE),
                         'running': sum(js.get(k, 0) for k in ('claimed', 'encoding', 'uploading', 'verifying'))},
               'scan': sc if sc else None}
        if movies:
            row.update(main_file.get(r['series'], {}))
        out.append(row)
    return out


def _queued():
    return db.one("SELECT COUNT(*) c FROM jobs WHERE status='queued'")['c']


@app.get('/api/status')
def status(user=Depends(ui)):
    """Etat leger pour l'en-tete (compteur de la file), interroge toutes les 3 s."""
    return {'queued': _queued(), 'paused': db.get_settings()['paused']}


@app.get('/api/series')
def series(user=Depends(ui)):
    s = db.get_settings()
    return {'series': _group_rows(), 'paused': s['paused'], 'sonarr': sonarr.configured(), 'queued': _queued()}


@app.get('/api/movies')
def movies(user=Depends(ui)):
    s = db.get_settings()
    return {'movies': _group_rows(movies=True), 'paused': s['paused'], 'radarr': radarr.configured(), 'queued': _queued(),
            'dir_ok': os.path.isdir(config.MOVIES_DIR)}


def _files(key):
    out = []
    last = {r['path']: r for r in db.q(
        'SELECT j.* FROM jobs j JOIN (SELECT path, MAX(id) m FROM jobs WHERE series=? GROUP BY path) x '
        'ON x.m=j.id', (key,))}
    for e in db.q('SELECT * FROM episodes WHERE series=? ORDER BY path', (key,)):
        p = json.loads(e['probe']) if e['probe'] else {}
        j = last.get(e['path'])
        plain = library.display(e['path'])
        out.append({'path': e['path'], 'file': plain.split('/', 1)[1] if '/' in plain else plain,
                    'size': e['size'], 'codec': e['codec'], 'height': p.get('height'), 'duration': p.get('duration'),
                    'bad': bool(e['bad']), 'issues': [i['msg'] for i in json.loads(e['issues'])],
                    'job': {'status': j['status'], 'progress': j['progress'], 'error': j['error']} if j else None})
    return out


@app.get('/api/series/{name}/episodes')
def episodes(name: str, user=Depends(ui)):
    return {'episodes': _files(name)}


@app.get('/api/movies/{name}/files')
def movie_files(name: str, user=Depends(ui)):
    return {'files': _files(library.movie_key(name))}


@app.get('/api/poster/{name}')
def poster(name: str, kind: str = '', user=Depends(ui)):
    """Affiche d'une serie, ou d'un film avec ?kind=movie : cache -> fichier local -> Sonarr/Radarr -> TMDB -> OMDb."""
    movie = kind == 'movie'
    key = library.movie_key(name) if movie else name
    hdr = {'Cache-Control': 'private, max-age=86400'}
    path, hit = posters.cached(key)
    if path:
        return FileResponse(path, media_type='image/jpeg', headers=hdr)
    if hit:
        raise HTTPException(404)
    try:
        root = library.abs_path(config.MOVIE_PREFIX + name if movie else name)
    except ValueError:
        raise HTTPException(404)
    names = ('poster.jpg', 'folder.jpg', 'cover.jpg', 'poster.png', 'folder.png')
    if movie:
        names = (name + '-poster.jpg', name + '.jpg') + names
    for f in names:
        p = os.path.join(root if os.path.isdir(root) else os.path.dirname(root), f)
        if (os.path.isdir(root) or f.startswith(name)) and os.path.isfile(p):
            return FileResponse(p, headers=hdr)
    data = None
    arr = radarr if movie else sonarr
    if arr.configured():
        try:
            data = arr.poster(name)
        except Exception:
            data = None
    data = data or posters.lookup(name, 'movie' if movie else 'tv')
    saved = posters.store(key, data)
    if saved:
        return FileResponse(saved, media_type='image/jpeg', headers=hdr)
    raise HTTPException(404)


@app.delete('/api/posters/cache')
def posters_clear(user=Depends(ui)):
    n = posters.clear_cache()
    journal.info('posters', 'cache des affiches vide : %d fichier(s)' % n)
    return {'removed': n}


@app.post('/api/settings/posters-test')
def posters_test(user=Depends(ui)):
    try:
        res = posters.test()
    except Exception as e:
        raise HTTPException(400, str(e))
    if not res:
        raise HTTPException(400, 'aucune cle renseignee')
    return res


@app.post('/api/actions')
async def actions(request: Request, user=Depends(ui)):
    """Corps : {action, series: [noms]} ; avec kind='movie', les noms sont des films (onglet Films)."""
    b = await _json(request)
    if not isinstance(b.get('series', []), list):
        raise HTTPException(400, 'corps invalide')
    # hors de la boucle d'evenements : la mise en file relit la bibliotheque et lance ffprobe, ce qui pouvait
    # figer tout le hub (interface et agents) pendant de longues secondes
    return await run_in_threadpool(_action, b, user)


def _action(b, user):
    movie = b.get('kind') == 'movie'
    act, shown = b.get('action'), [str(n) for n in b.get('series', [])]
    keys = [library.movie_key(n) for n in shown] if movie else shown
    what = 'film(s)' if movie else 'serie(s)'
    arr, arr_name = (radarr, 'Radarr') if movie else (sonarr, 'Sonarr')
    if act not in ('scan', 'encode', 'cancel', 'delete_bad', 'unmonitor'):
        journal.error('action', 'action inconnue recue de %s : %r' % (user, act))
        raise HTTPException(400, 'action inconnue : %r' % (act,))
    journal.info('action', '%s : %s sur %d %s : %s' % (user, act, len(shown), what, ', '.join(shown[:5]) + ('...' if len(shown) > 5 else '')))
    if act == 'scan':
        level = b.get('level') or db.get_settings()['scan']['level']
        n = library.enqueue_scan(keys, 'full' if level == 'full' else 'quick')
        journal.info('scan', 'scan %s lance : %d fichier(s) en attente' % (level, n))
        return {'queued': n}
    if act == 'encode':
        library.refresh()
        while library.probe_pending():
            pass
        rep = jobs.enqueue(keys, retry_bad=bool(b.get('retry_bad')))
        journal.info('queue', 'mise en file : %(enqueued)d ajoute(s), %(already_hevc)d deja HEVC, %(already_modern)d AV1/VP9 ignore(s), %(skipped_compact)d deja compact(s), %(skipped_bad)d defectueux ignore(s), %(already_queued)d deja en file, %(not_probed)d pas encore analyse(s)' % rep)
        return rep
    if act == 'unmonitor':
        if not arr.configured():
            raise HTTPException(400, "%s n'est pas configure (Parametres)" % arr_name)
        out = {'done': [], 'already': [], 'incomplete': {}, 'missing': [], 'errors': {}}
        for n, k in zip(shown, keys):
            ok, why = library.group_complete(k)
            if not ok:
                out['incomplete'][n] = why
                continue
            try:
                res, msg = radarr.unmonitor_movie(n) if movie else sonarr.unmonitor_series(n)
            except Exception as e:
                out['errors'][n] = str(e)
                continue
            {'fait': out['done'], 'deja': out['already'], 'absent': out['missing']}[res].append(n)
            if res == 'fait':
                journal.info(arr_name.lower(), '%s : suivi desactive dans %s (demande de %s)' % (n, arr_name, user))
        return out
    if act == 'cancel':
        n = jobs.cancel_series(keys)
        journal.info('queue', 'annulation : %d fichier(s) retire(s) de la file (les encodages en cours sont arretes)' % n)
        return {'canceled': n}
    if act == 'delete_bad':
        if not b.get('confirm'):
            raise HTTPException(400, 'confirmation requise')
        mode = db.get_settings()['bad_delete_mode']
        removed = {}
        for n, k in zip(shown, keys):
            for p in jobs.remove_bad([k], mode):
                removed.setdefault(n, []).append(os.path.basename(p))
        res = {'removed': sum(len(v) for v in removed.values()), 'mode': mode, 'sonarr': None, 'radarr': None}
        for n, files in removed.items():
            journal.info('action', 'fichiers defectueux retires (%s) de %s : %s' % (mode, n, ', '.join(files[:6])))
        if arr.configured() and removed:
            src = arr_name.lower()

            def relaunch():
                for n, files in removed.items():
                    try:
                        r = radarr.rescan_and_search(n) if movie else sonarr.rescan_and_search(n, files)
                        if not r.get('ok'):
                            journal.warn(src, '%s : %s' % (n, r.get('msg')))
                        elif movie:
                            journal.info(src, '%s : rescan fait%s' % (n, ', recherche lancee' if r.get('searched') else ' (rien a rechercher)'))
                        else:
                            journal.info(src, '%s : rescan fait, recherche lancee pour %s episode(s) sur %s' % (n, r.get('searched'), r.get('asked')))
                    except Exception as e:
                        journal.error(src, '%s : %s' % (n, e))
            threading.Thread(target=relaunch, daemon=True).start()
            res[src] = 'rescan puis recherche lances'
        return res
    raise HTTPException(400, 'action inconnue')


# ------------------------------------------------------------------ file d'attente
@app.get('/api/queue')
def queue(user=Depends(ui)):
    names = {h['id']: h['name'] for h in db.q('SELECT id,name FROM hosts')}
    rows = db.q("SELECT * FROM jobs WHERE status IN ('queued','claimed','encoding','uploading','verifying') "
                "ORDER BY batch,id LIMIT 500")
    rows += db.q("SELECT * FROM jobs WHERE status IN ('done','failed','canceled','skipped') ORDER BY finished DESC LIMIT 80")
    return {'paused': db.get_settings()['paused'], 'queued': _queued(),
            'jobs': [{'id': r['id'], 'series': library.display(r['series']), 'kind': 'movie' if library.is_movie(r['series']) else 'series',
                      'file': r['path'].rsplit('/', 1)[-1],
                      'status': r['status'], 'progress': r['progress'], 'phase': r['phase'], 'speed': r['speed'],
                      'host': names.get(r['host_id']), 'gpu': r['gpu'], 'error': r['error'], 'attempts': r['attempts'],
                      'in_size': r['src_size'], 'out_size': r['out_size']} for r in rows]}


@app.get('/api/logs')
def get_logs(request: Request, user=Depends(ui)):
    q = request.query_params
    try:
        limit, before = int(q.get('limit') or 300), int(q['before']) if q.get('before') else None
    except ValueError:
        raise HTTPException(400, 'limit / before : nombre attendu')
    return {'logs': journal.query(limit=limit, level=q.get('level'), source=q.get('source') or None,
                                  text=q.get('q') or None, before=before),
            'sources': [r['source'] for r in db.q('SELECT DISTINCT source FROM logs ORDER BY source')]}


@app.delete('/api/logs')
def clear_logs(request: Request, user=Depends(ui)):
    if request.query_params.get('confirm') != 'yes':
        raise HTTPException(400, 'confirmation requise')
    n = journal.clear()
    journal.warn('system', '%s vide le journal (%d ligne(s))' % (user, n))
    return {'removed': n}


@app.post('/api/jobs/{job_id}/cancel')
def job_cancel(job_id: int, user=Depends(ui)):
    jobs.cancel_job(job_id)
    journal.info('queue', '%s annule le job %d' % (user, job_id))
    return {'ok': True}


@app.post('/api/jobs/{job_id}/prioritize')
async def job_prioritize(job_id: int, request: Request, user=Depends(ui)):
    force = bool((await _json(request, strict=False)).get('force'))
    r = jobs.prioritize_job(job_id, force=force)
    if not r['ok']:
        raise HTTPException(409, 'ce fichier n\'est plus en attente')
    journal.info('queue', '%s %s le job %d%s' % (user, 'force' if force else 'met en priorite', job_id,
                 ' (job %d interrompu)' % r['preempted'] if r['preempted'] else ''))
    r['paused'] = db.get_settings()['paused']
    return r


@app.post('/api/jobs/{job_id}/retry')
def job_retry(job_id: int, user=Depends(ui)):
    ok = jobs.retry_job(job_id)
    journal.info('queue', '%s relance le job %d : %s' % (user, job_id, 'remis en file' if ok else 'impossible'))
    return {'ok': ok}


# ------------------------------------------------------------------ statistiques
@app.get('/api/stats')
def stats(user=Depends(ui)):
    """Espace gagne par les encodages valides (jobs 'done' avec taille avant/apres)."""
    done = "status='done' AND src_size>0 AND out_size>0"
    t = db.q("SELECT COUNT(*) n, COALESCE(SUM(src_size),0) a, COALESCE(SUM(out_size),0) b, MIN(finished) first, "
             "COALESCE(SUM(finished-started),0) busy FROM jobs WHERE " + done)[0]
    before, after = t['a'] or 0, t['b'] or 0
    ratio = (after / before) if before else None

    day = 86400
    daily = db.q("SELECT CAST(finished/%d AS INTEGER) d, COUNT(*) n, SUM(src_size-out_size) saved FROM jobs WHERE %s "
                 "AND finished IS NOT NULL GROUP BY d ORDER BY d" % (day, done))
    top = db.q("SELECT series, COUNT(*) n, SUM(src_size) a, SUM(out_size) b FROM jobs WHERE %s "
               "GROUP BY series ORDER BY SUM(src_size-out_size) DESC LIMIT 8" % done)
    hosts = db.q("SELECT COALESCE(h.name,'?') name, COUNT(*) n, SUM(j.src_size-j.out_size) saved FROM jobs j "
                 "LEFT JOIN hosts h ON h.id=j.host_id WHERE j.%s GROUP BY j.host_id ORDER BY saved DESC" % done.replace(' AND ', ' AND j.'))

    # Ce qui reste a encoder, estime avec le ratio mesure (repli : -50 %).
    todo = db.q("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM episodes "
                "WHERE probe IS NOT NULL AND bad=0 AND codec NOT IN " + _kept_sql())[0]
    r = ratio if ratio else 0.5
    return {
        'done': t['n'] or 0, 'before': before, 'after': after, 'saved': before - after,
        'percent': round(100 * (1 - ratio), 1) if ratio is not None else None,
        'busy_s': t['busy'] or 0, 'first': t['first'],
        'daily': [{'day': d['d'] * day, 'n': d['n'], 'saved': d['saved'] or 0} for d in daily],
        'series': [{'name': library.display(x['series']), 'movie': library.is_movie(x['series']), 'n': x['n'], 'before': x['a'], 'after': x['b'],
                    'saved': x['a'] - x['b']} for x in top],
        'hosts': [{'name': x['name'], 'n': x['n'], 'saved': x['saved'] or 0} for x in hosts],
        'todo': {'files': todo['n'] or 0, 'size': todo['s'] or 0, 'estimated_saving': int(todo['s'] * (1 - r)),
                 'ratio_used': round(r, 3), 'measured': ratio is not None},
    }


# ------------------------------------------------------------------ parametres
def _mask(s):
    s = json.loads(json.dumps(s))
    for k in ('sonarr', 'radarr'):
        if s[k]['api_key']:
            s[k]['api_key'] = '********'
    for k in ('tmdb_key', 'omdb_key'):
        if s['posters'][k]:
            s['posters'][k] = '********'
    return s


@app.get('/api/settings')
def get_settings(user=Depends(ui)):
    return {'settings': _mask(db.get_settings()), 'quarantine': jobs.quarantine_info()}


def _coerce(default, v):
    """Ramene une valeur au type de sa valeur par defaut (protege la base contre un fichier importe bancal)."""
    if isinstance(default, bool):
        if isinstance(v, str):
            return v.strip().lower() in ('1', 'true', 'oui', 'yes', 'on')
        return bool(v)
    if isinstance(default, int):
        return int(v)
    if isinstance(default, float):
        return float(v)
    if isinstance(default, str):
        return str(v)
    return v


def _clean_settings(b):
    """Valide et normalise un lot de parametres (formulaire ou fichier importe). Leve HTTPException(400)."""
    b = {k: v for k, v in b.items() if k in db.DEFAULTS and k != 'setup_done'}   # setup_done : reserve a /api/setup/*
    try:
        for k in list(b):
            d = db.DEFAULTS[k]
            if isinstance(d, dict):
                if not isinstance(b[k], dict):
                    raise HTTPException(400, '%s : objet attendu' % k)
                b[k] = {a: _coerce(d[a], v) for a, v in b[k].items() if a in d}
            else:
                b[k] = _coerce(d, b[k])
    except (TypeError, ValueError):
        raise HTTPException(400, 'valeur invalide dans les parametres')
    q = b.get('quality')
    if q:
        for k, hi in (('cq', 51), ('lookahead', 64), ('bframes', 4), ('cpu_crf', 51), ('maxrate_pct', 300)):
            if k in q:
                q[k] = max(0, min(hi, q[k]))
        if 'preset' in q and q['preset'] not in ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'):
            q['preset'] = 'p6'
        if 'tune' in q and q['tune'] not in ('hq', 'll', 'ull', 'lossless'):
            q['tune'] = 'hq'
        if 'cpu_preset' in q and q['cpu_preset'] not in ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
                                                         'medium', 'slow', 'slower', 'veryslow'):
            q['cpu_preset'] = 'medium'
    if b.get('replace_mode') not in (None, 'replace', 'quarantine'):
        raise HTTPException(400, 'replace_mode invalide')
    if b.get('bad_delete_mode') not in (None, 'delete', 'quarantine'):
        raise HTTPException(400, 'bad_delete_mode invalide')
    sc = b.get('scan')
    if sc:
        if 'level' in sc and sc['level'] not in ('quick', 'full'):
            sc['level'] = 'quick'
        if 'concurrency' in sc:
            sc['concurrency'] = max(1, min(16, sc['concurrency']))
    for k in ('sonarr', 'radarr'):
        sn = b.get(k)
        if sn:
            if sn.get('api_key') == '********':
                sn['api_key'] = db.get_settings()[k]['api_key']
            elif 'api_key' in sn:
                sn['api_key'] = sn['api_key'].strip()
            if 'url' in sn:
                sn['url'] = sn['url'].strip().rstrip('/')
    pc = b.get('posters')
    if pc:
        cur = db.get_settings()['posters']
        for k in ('tmdb_key', 'omdb_key'):
            if pc.get(k) == '********':
                pc[k] = cur[k]
            elif k in pc:
                pc[k] = pc[k].strip()
        if 'language' in pc and not re.fullmatch(r'[a-z]{2}-[A-Z]{2}', pc['language']):
            pc['language'] = 'fr-FR'
    if 'public_url' in b:
        b['public_url'] = b['public_url'].strip().rstrip('/')
        if b['public_url'] and not re.match(r'https?://[^/\s]+', b['public_url']):
            raise HTTPException(400, "URL publique du hub : elle doit commencer par http:// ou https://")
        if re.search(r'[\s\'"`$<>\\|;&(){}]', b['public_url']):   # elle est recopiee dans les scripts d'installation
            raise HTTPException(400, "URL publique du hub : caractere non autorise")
    for k in ('sonarr', 'radarr'):
        u = (b.get(k) or {}).get('url')
        if u and not re.match(r'https?://[^/\s]+', u):
            raise HTTPException(400, "URL %s : elle doit commencer par http:// ou https://" % k.capitalize())
    if 'tolerance_s' in b:
        b['tolerance_s'] = max(0.0, b['tolerance_s'])
    if 'min_gain_pct' in b:
        b['min_gain_pct'] = max(0, min(90, b['min_gain_pct']))
    if 'max_attempts' in b:
        b['max_attempts'] = max(1, min(10, b['max_attempts']))
    if 'lease_s' in b:      # bail d'un agent sans nouvelles : trop court, un transfert lent serait repris ailleurs
        b['lease_s'] = max(60, min(86400, b['lease_s']))
    av = b.get('av1')
    if isinstance(av, dict):
        if 'reencode' in av:
            av['reencode'] = bool(av['reencode'])
        if 'max_growth_pct' in av:
            av['max_growth_pct'] = min(1000, max(0, int(av['max_growth_pct'] or 0)))
    return b


@app.put('/api/settings')
async def put_settings(request: Request, user=Depends(ui)):
    b = _clean_settings(await _json(request))
    sonarr._cache['t'] = radarr._cache['t'] = 0
    db.set_settings(b)
    journal.info('action', '%s modifie les parametres : %s' % (user, ', '.join(sorted(b)) or '-'))
    return {'ok': True}


# ------------------------------------------------------------------ export / import de la configuration
@app.get('/api/settings/sections')
def settings_sections(user=Depends(ui)):
    return {'sections': backup.sections_info()}


@app.get('/api/settings/export')
def settings_export(request: Request, user=Depends(ui)):
    secs = [s for s in (request.query_params.get('sections') or '').split(',') if s] or None
    secrets = request.query_params.get('secrets') in ('1', 'true', 'yes')
    data = backup.export(secs, secrets)
    journal.info('action', '%s exporte la configuration (%s%s)' % (
        user, ', '.join(data['sections']) or '-', ', avec les cles API' if secrets else ''))
    name = 'reencodearr-config-%s.json' % time.strftime('%Y%m%d-%H%M')
    return JSONResponse(data, headers={'Content-Disposition': 'attachment; filename="%s"' % name})


@app.post('/api/settings/import')
async def settings_import(request: Request, user=Depends(ui)):
    """Corps : {data: <fichier exporte>, sections: [...] (facultatif), dry_run: bool}.

    Une cle API absente du fichier (export sans les cles) garde la valeur deja enregistree."""
    b = await _json(request)
    try:
        parsed = backup.parse(b.get('data'))
    except ValueError as e:
        raise HTTPException(400, str(e))
    found = [{'id': s, 'label': backup.SECTIONS[s][0], 'secrets': backup.has_secrets(parsed[s])} for s in parsed]
    meta = b['data']
    info = {'found': found, 'hub_version': str(meta.get('hub_version', '')),
            'exported_at': str(meta.get('exported_at', '')), 'secrets_included': bool(meta.get('secrets_included'))}
    if b.get('dry_run'):
        return {'ok': True, **info}
    wanted = b.get('sections')
    chosen = [s for s in parsed if not isinstance(wanted, list) or s in wanted]
    if not chosen:
        raise HTTPException(400, 'aucune categorie choisie')
    patch = {}
    for s in chosen:
        patch.update(parsed[s])
    patch = _clean_settings(patch)
    sonarr._cache['t'] = radarr._cache['t'] = 0
    db.set_settings(patch)
    journal.info('action', '%s importe la configuration : %s' % (user, ', '.join(chosen)))
    return {'ok': True, 'applied': chosen, **info}


@app.post('/api/settings/sonarr-test')
def sonarr_test(user=Depends(ui)):
    try:
        return {'ok': True, **sonarr.test()}
    except Exception as e:
        journal.error('sonarr', 'test de connexion : %s' % e)
        raise HTTPException(400, str(e))


@app.post('/api/settings/radarr-test')
def radarr_test(user=Depends(ui)):
    try:
        return {'ok': True, **radarr.test()}
    except Exception as e:
        journal.error('radarr', 'test de connexion : %s' % e)
        raise HTTPException(400, str(e))


@app.delete('/api/quarantine')
def purge(request: Request, user=Depends(ui)):
    if request.query_params.get('confirm') != 'yes':
        raise HTTPException(400, 'confirmation requise')
    info = jobs.purge_quarantine()
    journal.warn('action', '%s vide la quarantaine : %d fichier(s) supprime(s) definitivement' % (user, info['files']))
    return info


# ------------------------------------------------------------------ hotes
def _host_view(h, hub_url):
    info = json.loads(h['info'] or '{}')
    running = db.q("SELECT gpu, series, path, progress, phase FROM jobs WHERE host_id=? AND status IN "
                   "('claimed','encoding','uploading')", (h['id'],))
    return {'id': h['id'], 'name': h['name'], 'enabled': bool(h['enabled']),
            'online': bool(h['last_seen'] and time.time() - h['last_seen'] < 45),
            'last_seen': h['last_seen'], 'gpus': info.get('gpus', []),
            'gpus_enabled': json.loads(h['gpus_enabled'] or '[]'),
            'platform': info.get('platform'), 'agent_version': info.get('version'),
            'update': agents.view(h, info, hub_url),
            'ffmpeg': info.get('ffmpeg'), 'nvenc': info.get('nvenc'), 'nvenc_error': info.get('nvenc_error'),
            'reusable_key': bool(db.one('SELECT 1 FROM enroll_codes WHERE host_id=?', (h['id'],))),
            'running': [{'gpu': r['gpu'], 'series': library.display(r['series']), 'file': r['path'].rsplit('/', 1)[-1],
                         'progress': r['progress'], 'phase': r['phase']} for r in running]}


@app.get('/api/hosts')
def hosts(request: Request, user=Depends(ui)):
    url = base_url(request)
    return {'hosts': [_host_view(h, url) for h in db.q('SELECT * FROM hosts ORDER BY name')],
            'agent_latest': agents.latest()}


@app.post('/api/hosts/enroll')
async def enroll(request: Request, user=Depends(ui)):
    """Commande d'installation d'un host. Corps facultatif : {"os": "linux" | "windows", "reusable": bool}.
    reusable : machine non persistante qui relance la commande a chaque demarrage et retrouve le meme host."""
    b = await _json(request, strict=False)
    windows = b.get('os') == 'windows'
    reusable = bool(b.get('reusable'))
    code = security.new_enroll_code(reusable)
    url = base_url(request)
    journal.info('agent', '%s genere %s pour %s (%s)' % (
        user, 'une cle reutilisable' if reusable else 'un code d enrolement (valable 1 h)', url,
        'Windows' if windows else 'Linux'))
    if windows:
        command = ("powershell -NoProfile -ExecutionPolicy Bypass -Command \"irm '%s/enroll/%s/windows' | iex\""
                   % (_ps(url), code))
    else:
        command = "curl -fsSL %s | sudo bash" % shlex.quote('%s/enroll/%s' % (url, code))
    return {'command': command, 'expires_in': security.REUSABLE_FIRST_USE if reusable else 3600, 'url': url,
            'os': 'windows' if windows else 'linux', 'reusable': reusable}


@app.patch('/api/hosts/{host_id}')
async def host_patch(host_id: int, request: Request, user=Depends(ui)):
    b = await _json(request)
    h = db.one('SELECT * FROM hosts WHERE id=?', (host_id,))
    if not h:
        raise HTTPException(404)
    if 'gpus_enabled' in b and not isinstance(b['gpus_enabled'], list):
        raise HTTPException(400, 'gpus_enabled : liste attendue')
    if 'enabled' in b:
        db.run('UPDATE hosts SET enabled=? WHERE id=?', (1 if b['enabled'] else 0, host_id))
    if 'gpus_enabled' in b:
        known = {str(g['id']) for g in json.loads(h['info'] or '{}').get('gpus', [])}
        wanted = [str(g) for g in b['gpus_enabled'] if str(g) in known]
        db.run('UPDATE hosts SET gpus_enabled=? WHERE id=?', (json.dumps(wanted), host_id))
    if b.get('name'):
        try:
            db.run('UPDATE hosts SET name=? WHERE id=?', (str(b['name'])[:60], host_id))
        except Exception:
            raise HTTPException(400, 'nom deja utilise')
    journal.info('agent', '%s modifie le host %d : %s' % (user, host_id, json.dumps(
        {k: b[k] for k in ('enabled', 'gpus_enabled', 'name') if k in b})[:300]))
    return {'ok': True}


@app.post('/api/hosts/{host_id}/update')
def host_update(host_id: int, user=Depends(ui)):
    h = db.one('SELECT * FROM hosts WHERE id=?', (host_id,))
    if not h:
        raise HTTPException(404)
    if not (h['last_seen'] and time.time() - h['last_seen'] < 45):
        raise HTTPException(400, 'host hors ligne : la mise a jour se fait quand l agent est connecte')
    return {'ok': True, **agents.request(h, user)}


@app.delete('/api/hosts/{host_id}')
def host_delete(host_id: int, user=Depends(ui)):
    for j in db.q("SELECT id FROM jobs WHERE host_id=? AND status IN ('claimed','encoding','uploading')", (host_id,)):
        jobs.fail(j['id'], host_id, 'hote supprime')
    db.run('DELETE FROM hosts WHERE id=?', (host_id,))
    db.run('DELETE FROM enroll_codes WHERE host_id=?', (host_id,))   # revoque sa cle reutilisable
    journal.warn('agent', '%s retire le host %d' % (user, host_id))
    return {'ok': True}


# ------------------------------------------------------------------ cote agent
ENROLL_SH = r'''#!/bin/bash
# Installation de l'agent Reencodearr (genere par le hub, code a usage unique).
set -euo pipefail
HUB=__HUB__
CODE=__CODE__
FFMPEG_URL=__FFURL__
DIR=/opt/reencodearr-agent
[ "$(id -u)" = 0 ] || { echo "Lancez ce script en root (sudo)."; exit 1; }
command -v python3 >/dev/null || { echo "python3 est requis."; exit 1; }
command -v curl >/dev/null || { echo "curl est requis."; exit 1; }
mkdir -p "$DIR"
curl -fsSL "$HUB/agent/agent.py" -o "$DIR/agent.py"
FF="$(command -v ffmpeg || true)"
if [ -z "$FF" ] || ! "$FF" -hide_banner -encoders 2>/dev/null | grep -q hevc_nvenc; then
  echo "ffmpeg avec NVENC absent : telechargement d'une version statique..."
  command -v xz >/dev/null || { echo "xz-utils est requis (apt install xz-utils)."; exit 1; }
  curl -fsSL "$FFMPEG_URL" -o "$DIR/ffmpeg.tar.xz"
  tar -xJf "$DIR/ffmpeg.tar.xz" -C "$DIR" --strip-components=2 --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'
  rm -f "$DIR/ffmpeg.tar.xz"
  FF="$DIR/ffmpeg"
fi
python3 "$DIR/agent.py" register --hub "$HUB" --code "$CODE" --name "$(hostname)" --ffmpeg "$FF" --dir "$DIR"
cat > /etc/systemd/system/reencodearr-agent.service <<UNIT
[Unit]
Description=Reencodearr agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/env python3 $DIR/agent.py run --dir $DIR
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now reencodearr-agent
echo "Agent installe. Il apparait dans l'onglet Hosts du hub."
'''


# Script PowerShell (Windows 10/11, Windows Server 2016 et plus). Relancable : chaque execution remet l'agent a la
# version du hub. Avec une cle reutilisable, une tache planifiee relit ce script a chaque demarrage : une VM non
# persistante se reinstalle et retrouve le meme host. Le script reste en ASCII (PowerShell 5.1 lit mal l'UTF-8).
ENROLL_PS1 = r"""# Installation de l'agent Reencodearr sous Windows (genere par le hub).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
$Hub = '__HUB__'
$Code = '__CODE__'
$Reusable = $__REUSABLE__
$FfmpegUrl = '__FFURL__'
$PythonUrl = '__PYURL__'
$Dir = Join-Path $env:ProgramData 'Reencodearr-agent'
$TaskName = 'Reencodearr agent'
$Py = Join-Path $Dir 'python\python.exe'
$Agent = Join-Path $Dir 'agent.py'

$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Lancez cette commande dans un PowerShell ouvert en administrateur.'
}

function Invoke-Native([string]$exe, [string[]]$params) {
  # sortie d'un programme sans que PowerShell 5.1 prenne son stderr pour une erreur
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $out = & $exe @params 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $old }
  return @($out)
}

function Install-Agent {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  if (-not (Test-Path $Py)) {
    Write-Host 'Telechargement de Python (version embarquee, sans installation)...'
    $zip = Join-Path $Dir 'python.zip'
    Invoke-WebRequest -UseBasicParsing -Uri $PythonUrl -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $Dir 'python') -Force
    Remove-Item $zip -Force
  }
  Invoke-WebRequest -UseBasicParsing -Uri "$Hub/agent/agent.py" -OutFile $Agent
  $script:Ff = Join-Path $Dir 'ffmpeg\ffmpeg.exe'
  if (-not (Test-Path $script:Ff)) {
    $sys = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($sys -and ((Invoke-Native $sys.Source @('-hide_banner', '-encoders')) -match 'hevc_nvenc')) {
      $script:Ff = $sys.Source
    } else {
      Write-Host 'Telechargement de ffmpeg avec NVENC...'
      $zip = Join-Path $Dir 'ffmpeg.zip'; $tmp = Join-Path $Dir 'ffmpeg-tmp'
      Invoke-WebRequest -UseBasicParsing -Uri $FfmpegUrl -OutFile $zip
      if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
      Expand-Archive -Path $zip -DestinationPath $tmp -Force
      New-Item -ItemType Directory -Force -Path (Join-Path $Dir 'ffmpeg') | Out-Null
      Get-ChildItem $tmp -Recurse -Include ffmpeg.exe, ffprobe.exe | Move-Item -Destination (Join-Path $Dir 'ffmpeg') -Force
      Remove-Item $zip, $tmp -Recurse -Force
    }
  }
  if (-not (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
    Write-Host 'Attention : nvidia-smi introuvable. Sans pilote NVIDIA, seul le CPU (lent) pourra encoder.' -ForegroundColor Yellow
  }
}

function Register-Agent {
  $out = Invoke-Native $Py @($Agent, 'register', '--hub', $Hub, '--code', $Code, '--name', $env:COMPUTERNAME,
                              '--ffmpeg', $script:Ff, '--dir', $Dir)
  $out | Write-Host
  if ($LASTEXITCODE -ne 0) { throw 'Enregistrement aupres du hub refuse.' }
}

function Stop-Agent {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $Py } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# boucle qui fait tourner l'agent ; apres une mise a jour (bouton du hub), l'agent s'arrete et repart ici
$run = @'
$ErrorActionPreference = 'Continue'
$Dir = '__DIR__'
$Hub = '__HUB__'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
$env:PYTHONIOENCODING = 'utf-8'
while ($true) {
  try { Invoke-WebRequest -UseBasicParsing -Uri "$Hub/agent/agent.py" -OutFile "$Dir\agent.py.new"
        Move-Item "$Dir\agent.py.new" "$Dir\agent.py" -Force } catch { }
  if (Test-Path "$Dir\agent.log") { Move-Item "$Dir\agent.log" "$Dir\agent.prev.log" -Force }
  if (Test-Path "$Dir\agent.err.log") { Move-Item "$Dir\agent.err.log" "$Dir\agent.prev.err.log" -Force }
  Start-Process -FilePath "$Dir\python\python.exe" -ArgumentList "-u `"$Dir\agent.py`" run --dir `"$Dir`"" `
    -WorkingDirectory $Dir -NoNewWindow -Wait -RedirectStandardOutput "$Dir\agent.log" -RedirectStandardError "$Dir\agent.err.log"
  Start-Sleep -Seconds 5
}
'@
# cle reutilisable : a chaque demarrage, relire ce script sur le hub (reinstallation, nouvel enregistrement)
$boot = @'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
$ReencodearrBoot = $true
while ($true) {
  try { Invoke-Expression (Invoke-RestMethod -UseBasicParsing -Uri '__HUB__/enroll/__CODE__/windows') }
  catch { Write-Host $_; Start-Sleep -Seconds 30 }
}
'@

Install-Agent
$run.Replace('__DIR__', $Dir) | Set-Content -Path (Join-Path $Dir 'run.ps1') -Encoding ASCII
if ($ReencodearrBoot) {
  # demarrage de la machine (tache planifiee, cle reutilisable) : nouvel enregistrement puis l'agent tourne
  Register-Agent
  & (Join-Path $Dir 'run.ps1')
  return
}

Stop-Agent
if ($Reusable) {
  $boot | Set-Content -Path (Join-Path $Dir 'boot.ps1') -Encoding ASCII
  $file = 'boot.ps1'
} else {
  Register-Agent
  $file = 'run.ps1'
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $Dir $file)`""
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
  -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Agent installe dans $Dir (tache planifiee '$TaskName', compte SYSTEM, au demarrage)."
Write-Host "Il apparait dans l'onglet Hosts du hub. Journal : $Dir\agent.log"
"""


def _ps(value):
    """Chaine PowerShell entre apostrophes."""
    return str(value).replace("'", "''")


@app.get('/enroll/{code}/windows', response_class=PlainTextResponse)
async def enroll_script_windows(code: str, request: Request):
    if not security.code_valid(code):
        raise HTTPException(404, 'code invalide ou expire')
    reusable = bool(db.one('SELECT reusable FROM enroll_codes WHERE code_hash=?', (security.sha(code),))['reusable'])
    return (ENROLL_PS1.replace('__HUB__', _ps(base_url(request))).replace('__CODE__', _ps(code))
            .replace('$__REUSABLE__', '$true' if reusable else '$false')
            .replace('__FFURL__', _ps(FFMPEG_URL_WINDOWS)).replace('__PYURL__', _ps(PYTHON_URL_WINDOWS)))


@app.get('/enroll/{code}', response_class=PlainTextResponse)
async def enroll_script(code: str, request: Request):
    if not security.code_valid(code):
        raise HTTPException(404, 'code invalide ou expire')
    return ENROLL_SH.replace('__HUB__', shlex.quote(base_url(request))).replace(
        '__CODE__', shlex.quote(code)).replace('__FFURL__', shlex.quote(FFMPEG_URL))


@app.get('/agent/agent.py')
async def agent_file():
    return FileResponse(AGENT_FILE, media_type='text/x-python')


@app.post('/api/agent/register')
async def agent_register(request: Request):
    b = await _json(request)
    code = str(b.get('code', ''))
    key = security.consume_code(code)
    if not key:
        raise HTTPException(403, 'code invalide, expire ou deja utilise')
    old = key['host_id'] and db.one('SELECT * FROM hosts WHERE id=?', (key['host_id'],))
    if old:
        # cle reutilisable : la machine (VM non persistante) s'est reinstallee. Meme host, nouveau jeton : l'ancien
        # processus ne peut plus rien faire, ses jobs repartent en file sans tentative perdue.
        token = security.new_host_token()
        info = json.loads(old['info'] or '{}')
        info.update(_clean_info(b.get('info')))
        db.run('UPDATE hosts SET token_hash=?,info=?,last_seen=?,agent_update=NULL WHERE id=?',
               (security.sha(token), json.dumps(info), time.time(), old['id']))
        for j in db.q("SELECT id FROM jobs WHERE host_id=? AND status IN %s" % jobs.RUNNING_SQL, (old['id'],)):
            db.run('UPDATE jobs SET cancel=4 WHERE id=?', (j['id'],))
            jobs.fail(j['id'], old['id'], 'machine redemarree')
        journal.info('agent', '%s s est reenregistre (cle reutilisable)' % old['name'])
        return {'token': token, 'host_id': old['id'], 'name': old['name']}
    name = re.sub(r'[\x00-\x1f\x7f]', '', str(b.get('name') or 'host'))[:50].strip() or 'host'
    base, i = name, 1
    while db.one('SELECT 1 FROM hosts WHERE name=?', (name,)):
        i += 1
        name = '%s-%d' % (base, i)
    token = security.new_host_token()
    hid = db.run('INSERT INTO hosts(name,token_hash,enabled,gpus_enabled,info,last_seen,created) VALUES(?,?,?,?,?,?,?)',
                 (name, security.sha(token), 1, '[]', json.dumps(_clean_info(b.get('info'))), time.time(), time.time()))
    if key['reusable']:
        security.bind_code(code, hid)
    journal.info('agent', 'nouveau host enregistre : %s (id %d)%s' % (name, hid, ' avec une cle reutilisable'
                                                                      if key['reusable'] else ''))
    return {'token': token, 'host_id': hid, 'name': name}


@app.post('/api/agent/heartbeat')
async def agent_heartbeat(request: Request, host=Depends(agent)):
    b = await _json(request)
    info = _clean_info(b.get('info'))
    first = not json.loads(host['info'] or '{}').get('gpus') and info.get('gpus')
    gpus_enabled = host['gpus_enabled']
    if first:   # premiere detection : on active les vrais GPU (pas le worker CPU)
        gpus_enabled = json.dumps([str(g['id']) for g in info['gpus'] if str(g['id']) != 'cpu'])
    old = json.loads(host['info'] or '{}')
    if info.get('nvenc') is False and old.get('nvenc_error') != info.get('nvenc_error'):
        journal.error('agent', '%s : NVENC ne fonctionne pas : %s' % (host['name'], info.get('nvenc_error')))
    elif info.get('nvenc') and old.get('nvenc') is False:
        journal.info('agent', '%s : NVENC fonctionne de nouveau' % host['name'])
    if first:
        journal.info('agent', '%s : GPU detectes : %s' % (host['name'], ', '.join('%s (%s)' % (g['name'], g['id']) for g in info['gpus'])))
    db.run('UPDATE hosts SET info=?,gpus_enabled=? WHERE id=?', (json.dumps(info), gpus_enabled, host['id']))
    todo = agents.on_heartbeat(host, info)
    if todo == 'kick':      # agent < 1.4.0 : il s'arrete sur un 401 et Docker le relance avec le nouvel agent.py
        raise HTTPException(401, 'mise a jour : redemarrage demande')
    return {'enabled': bool(host['enabled']), 'gpus_enabled': json.loads(gpus_enabled or '[]'),
            'paused': db.get_settings()['paused'], 'update': todo == 'update'}


@app.post('/api/agent/claim')
async def agent_claim(request: Request, host=Depends(agent)):
    b = await _json(request)
    if agents.pending(host):    # mise a jour en cours : plus de nouveau job pour ce host
        return {'job': None}
    job = jobs.claim(host, str(b.get('gpu')))
    return {'job': job}


def _token(request):
    return request.headers.get('X-Job-Token') or None


def _own_job(job_id, host, request, statuses=('claimed', 'encoding', 'uploading')):
    j = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not jobs.owns(j, host['id'], _token(request)) or j['status'] not in statuses:
        raise HTTPException(409, 'job non attribue a cet hote')
    return j


@app.get('/api/agent/jobs/{job_id}/source')
async def agent_source(job_id: int, request: Request, host=Depends(agent)):
    j = _own_job(job_id, host, request)
    return FileResponse(library.abs_path(j['path']), media_type='application/octet-stream')


@app.post('/api/agent/jobs/{job_id}/progress')
async def agent_progress(job_id: int, request: Request, host=Depends(agent)):
    b = await _json(request)
    try:
        pct = max(0.0, min(100.0, float(b.get('progress') or 0)))
    except (TypeError, ValueError):
        pct = 0.0
    cancel = jobs.progress(job_id, host['id'], pct, str(b.get('phase', ''))[:20],
                           str(b.get('speed', ''))[:20], _token(request))
    return {'cancel': cancel}


@app.put('/api/agent/jobs/{job_id}/result')
async def agent_result(job_id: int, request: Request, host=Depends(agent)):
    j = _own_job(job_id, host, request)
    # Un resultat valide est plus petit que la source ; l'AV1 -> HEVC peut grossir de 1000 % au plus (reglage).
    # Au-dela, c'est un envoi anormal qui ne doit pas pouvoir remplir le disque du hub.
    limit = (j['src_size'] or 0) * 11 + (256 << 20)
    expected = request.headers.get('content-length')
    if expected and expected.isdigit() and int(expected) > limit:
        raise HTTPException(413, 'resultat trop volumineux')
    part = jobs.upload_part(job_id)
    received = 0
    try:
        with open(part, 'wb') as f:
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit:
                    raise IOError('resultat trop volumineux')
                f.write(chunk)
        if expected and expected.isdigit() and received != int(expected):
            raise IOError('%d octets recus sur %s' % (received, expected))
    except Exception:
        jobs._rm(part)
        raise HTTPException(400, 'envoi interrompu')
    # le bail a pu expirer pendant un long envoi et le job etre repris ailleurs : on reverifie avant d'accepter
    if not jobs.accept_upload(job_id, host['id'], _token(request), part):
        journal.warn('queue', 'job %d : resultat de %s refuse, le job ne lui appartient plus' % (job_id, host['name']))
        raise HTTPException(409, 'job non attribue a cet hote')
    journal.info('queue', 'job %d : fichier encode recu de %s (%.2f Go), verification en cours' % (
        job_id, host['name'], received / 1e9))
    return {'status': 'verifying'}


@app.post('/api/agent/jobs/{job_id}/fail')
async def agent_fail(job_id: int, request: Request, host=Depends(agent)):
    b = await _json(request)
    jobs.fail(job_id, host['id'], str(b.get('reason', 'echec'))[:300], final=bool(b.get('final')), token=_token(request))
    return {'ok': True}


# ------------------------------------------------------------------ interface
app.mount('/static', StaticFiles(directory=os.path.join(HERE, 'static')), name='static')


@app.get('/')
async def index():
    return FileResponse(os.path.join(HERE, 'static', 'index.html'), headers={'Cache-Control': 'no-cache'})


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
