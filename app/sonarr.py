"""Client Sonarr minimal (API v3) : affiches et relance de recherche apres suppression."""
import json
import re
import time
import urllib.request

from . import db, journal, library

_cache = {'t': 0, 'series': []}


class SonarrError(Exception):
    pass


def cfg():
    return db.get_settings()['sonarr']


def configured():
    c = cfg()
    return bool(c['url'] and c['api_key'])


def _req(path, method='GET', body=None, raw=False, timeout=20):
    c = cfg()
    if not (c['url'] and c['api_key']):
        raise SonarrError('Sonarr non configure')
    req = urllib.request.Request(c['url'].rstrip('/') + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-Api-Key': c['api_key'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:
        raise SonarrError(str(e))
    return data if raw else (json.loads(data) if data else None)


def series_list(max_age=600):
    if time.time() - _cache['t'] > max_age:
        _cache['series'] = _req('/api/v3/series')
        _cache['t'] = time.time()
    return _cache['series']


def find_series(folder):
    """Retrouve la serie Sonarr dont le dossier porte ce nom."""
    f = folder.lower()
    for s in series_list():
        if (s.get('path') or '').replace('\\', '/').rstrip('/').split('/')[-1].lower() == f:
            return s
    for s in series_list():
        if s.get('title', '').lower() == f:
            return s
    return None


def poster(folder):
    s = find_series(folder)
    if not s:
        return None
    for name in ('poster-250.jpg', 'poster.jpg'):
        try:
            return _req('/api/v3/mediacover/%d/%s' % (s['id'], name), raw=True)
        except SonarrError:
            continue
    return None


def test():
    st = _req('/api/v3/system/status')
    if st.get('appName') and st['appName'].lower() != 'sonarr':
        raise SonarrError('cette adresse repond comme %s, pas comme Sonarr' % st['appName'])
    return {'version': st.get('version'), 'series': len(series_list(0))}


SE = re.compile(r'S(\d{1,3})E(\d{1,4})', re.I)


def season_episode(filename):
    m = SE.search(filename)
    return (int(m.group(1)), int(m.group(2))) if m else None


def rescan_and_search(folder, removed_names):
    """Apres suppression locale : rescan de la serie, puis EpisodeSearch des episodes devenus sans fichier."""
    s = find_series(folder)
    if not s:
        return {'ok': False, 'msg': 'serie introuvable dans Sonarr'}
    wanted = {season_episode(n) for n in removed_names if season_episode(n)}
    cmd = _req('/api/v3/command', 'POST', {'name': 'RescanSeries', 'seriesId': s['id']})
    for _ in range(45):
        time.sleep(2)
        st = _req('/api/v3/command/%d' % cmd['id'])
        if st.get('status') in ('completed', 'failed', 'aborted'):
            break
    eps = _req('/api/v3/episode?seriesId=%d' % s['id'])
    ids = [e['id'] for e in eps if (e['seasonNumber'], e['episodeNumber']) in wanted
           and not e.get('hasFile') and e.get('monitored')]
    if ids:
        _req('/api/v3/command', 'POST', {'name': 'EpisodeSearch', 'episodeIds': ids})
    return {'ok': True, 'searched': len(ids), 'asked': len(wanted)}


# ------------------------------------------------------------------ fin de reencodage -> fin du suivi
def series_complete(folder):
    return library.group_complete(folder)


def unmonitor_series(folder):
    """Passe la serie en 'non surveillee' dans Sonarr. Retourne (resultat, message)."""
    s = find_series(folder)
    if not s:
        return 'absent', 'serie introuvable dans Sonarr'
    full = _req('/api/v3/series/%d' % s['id'])
    if not full.get('monitored'):
        return 'deja', 'deja non surveillee'
    full['monitored'] = False
    _req('/api/v3/series/%d' % s['id'], 'PUT', full)
    _cache['t'] = 0
    return 'fait', 'suivi desactive'


def on_series_progress(folder):
    """Appelee apres chaque episode termine : si la serie est finie, coupe le suivi Sonarr."""
    try:
        if not (configured() and cfg().get('unmonitor_done', True)):
            return
        ok, why = series_complete(folder)
        if not ok:
            return
        res, msg = unmonitor_series(folder)
        if res == 'fait':
            journal.info('sonarr', '%s : serie entierement reencodee, suivi desactive dans Sonarr' % folder)
        elif res == 'absent':
            journal.warn('sonarr', '%s : serie terminee mais %s' % (folder, msg))
    except Exception as e:
        journal.error('sonarr', '%s : desactivation du suivi impossible : %s' % (folder, e))
