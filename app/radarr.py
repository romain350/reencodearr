"""Client Radarr minimal (API v3) : affiches des films, relance de recherche apres suppression, fin du suivi."""
import json
import os
import time
import urllib.request

from . import db, journal, library

_cache = {'t': 0, 'movies': []}


class RadarrError(Exception):
    pass


def cfg():
    return db.get_settings()['radarr']


def configured():
    c = cfg()
    return bool(c['url'] and c['api_key'])


def _req(path, method='GET', body=None, raw=False, timeout=20):
    c = cfg()
    if not (c['url'] and c['api_key']):
        raise RadarrError('Radarr non configure')
    req = urllib.request.Request(c['url'].rstrip('/') + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-Api-Key': c['api_key'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:
        raise RadarrError(str(e))
    return data if raw else (json.loads(data) if data else None)


def movie_list(max_age=600):
    if time.time() - _cache['t'] > max_age:
        _cache['movies'] = _req('/api/v3/movie')
        _cache['t'] = time.time()
    return _cache['movies']


def _base(p):
    return (p or '').replace('\\', '/').rstrip('/').split('/')[-1].lower()


def find_movie(folder):
    """Retrouve le film Radarr d'un dossier (ou d'un fichier pose a la racine) : dossier, fichier, puis titre."""
    f = folder.lower()
    movies = movie_list()
    for m in movies:
        if _base(m.get('path')) == f:
            return m
    for m in movies:
        mf = (m.get('movieFile') or {}).get('relativePath') or ''
        if mf and os.path.splitext(_base(mf))[0] == f:
            return m
    for m in movies:
        t = (m.get('title') or '').lower()
        if f in (t, '%s (%s)' % (t, m.get('year'))):
            return m
    return None


def poster(folder):
    m = find_movie(folder)
    if not m:
        return None
    for name in ('poster-250.jpg', 'poster.jpg'):
        try:
            return _req('/api/v3/mediacover/%d/%s' % (m['id'], name), raw=True)
        except RadarrError:
            continue
    return None


def test():
    st = _req('/api/v3/system/status')
    if st.get('appName') and st['appName'].lower() != 'radarr':
        raise RadarrError('cette adresse repond comme %s, pas comme Radarr' % st['appName'])
    return {'version': st.get('version'), 'movies': len(movie_list(0) or [])}


def rescan_and_search(folder):
    """Apres suppression locale : rescan du film, puis recherche s'il est surveille et n'a plus de fichier."""
    m = find_movie(folder)
    if not m:
        return {'ok': False, 'msg': 'film introuvable dans Radarr'}
    cmd = _req('/api/v3/command', 'POST', {'name': 'RescanMovie', 'movieId': m['id']})
    for _ in range(45):
        time.sleep(2)
        st = _req('/api/v3/command/%d' % cmd['id'])
        if st.get('status') in ('completed', 'failed', 'aborted'):
            break
    full = _req('/api/v3/movie/%d' % m['id'])
    _cache['t'] = 0
    if full.get('hasFile') or not full.get('monitored'):
        return {'ok': True, 'searched': 0}
    _req('/api/v3/command', 'POST', {'name': 'MoviesSearch', 'movieIds': [m['id']]})
    return {'ok': True, 'searched': 1}


def unmonitor_movie(folder):
    """Passe le film en 'non surveille' dans Radarr. Retourne (resultat, message)."""
    m = find_movie(folder)
    if not m:
        return 'absent', 'film introuvable dans Radarr'
    full = _req('/api/v3/movie/%d' % m['id'])
    if not full.get('monitored'):
        return 'deja', 'deja non surveille'
    full['monitored'] = False
    _req('/api/v3/movie/%d' % m['id'], 'PUT', full)
    _cache['t'] = 0
    return 'fait', 'suivi desactive'


def on_movie_progress(key):
    """Appelee apres chaque fichier termine : si le film est fini, coupe le suivi Radarr."""
    name = library.display(key)
    try:
        if not (configured() and cfg().get('unmonitor_done', True)):
            return
        ok, why = library.group_complete(key)
        if not ok:
            return
        res, msg = unmonitor_movie(name)
        if res == 'fait':
            journal.info('radarr', '%s : film entierement reencode, suivi desactive dans Radarr' % name)
        elif res == 'absent':
            journal.warn('radarr', '%s : film termine mais %s' % (name, msg))
    except Exception as e:
        journal.error('radarr', '%s : desactivation du suivi impossible : %s' % (name, e))
