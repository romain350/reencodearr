"""Affiches : TMDB (series et films) et OMDb (donnees IMDb), avec cache disque.

Ordre de recherche (voir main.poster) : cache -> fichier local -> Sonarr ou Radarr -> TMDB -> OMDb.
Le cache garde les resultats positifs 30 jours et les echecs 1 jour (pour ne pas interroger
les services a chaque affichage).
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from . import config, db, journal

POSITIVE_TTL = 30 * 86400
NEGATIVE_TTL = 86400
NOISE = re.compile(r'\b(1080p|720p|2160p|4k|web|webrip|bluray|hdtv|multi|french|vostfr|truefrench|x26[45]|h26[45]|hevc)\b',
                   re.I)


def cache_key(folder):
    return hashlib.sha256(folder.encode()).hexdigest()[:24]


def cached(folder):
    """Retourne (octets|None, trouve_en_cache). None + True = echec memorise recemment."""
    base = os.path.join(config.POSTER_DIR, cache_key(folder))
    jpg, none = base + '.jpg', base + '.none'
    if os.path.exists(jpg) and time.time() - os.path.getmtime(jpg) < POSITIVE_TTL:
        return jpg, True
    if os.path.exists(none) and time.time() - os.path.getmtime(none) < NEGATIVE_TTL:
        return None, True
    return None, False


def store(folder, data):
    base = os.path.join(config.POSTER_DIR, cache_key(folder))
    if data:
        tmp = '%s.%d.tmp' % (base, threading.get_ident())     # deux affichages simultanes : jamais d'image a moitie ecrite
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, base + '.jpg')
        if os.path.exists(base + '.none'):
            os.remove(base + '.none')
        return base + '.jpg'
    open(base + '.none', 'w').close()
    return None


def clear_cache():
    n = 0
    if os.path.isdir(config.POSTER_DIR):
        for f in os.listdir(config.POSTER_DIR):
            if f.endswith(('.jpg', '.none')):
                os.remove(os.path.join(config.POSTER_DIR, f))
                n += 1
    return n


def clean_title(folder):
    """'Iron Man 3 (2013)' -> ('Iron Man 3', 2013) ; retire points, tags de qualite, crochets."""
    t = NOISE.sub(' ', folder.replace('.', ' ').replace('_', ' '))
    year = None
    m = re.search(r'[\(\[ ]*(19\d\d|20[0-3]\d)[\)\] ]*$', t.strip())
    if m and t.strip()[:m.start()].strip():
        year = int(m.group(1))
        t = t.strip()[:m.start()]
    t = re.sub(r'[\(\[].*?[\)\]]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip() or folder, year


MAX_BYTES = 20 << 20      # une affiche ou une reponse d'API ne depasse jamais cette taille


def _get(url, headers=None, timeout=15):
    # l'adresse d'une affiche vient de la reponse d'un service tiers : jamais de file:// ni d'autre schema
    if not str(url).lower().startswith(('https://', 'http://')):
        raise ValueError('adresse refusee (http ou https attendu)')
    req = urllib.request.Request(url, headers=dict({'User-Agent': 'Reencodearr/1.1'}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('reponse trop volumineuse')
    return data


def _tmdb(path, params):
    cfg = db.get_settings()['posters']
    key = cfg['tmdb_key'].strip()
    headers = {}
    params = dict(params, language=cfg['language'])
    if key.startswith('eyJ'):                      # jeton de lecture v4
        headers['Authorization'] = 'Bearer ' + key
    else:
        params['api_key'] = key
    return json.loads(_get('https://api.themoviedb.org/3' + path + '?' + urllib.parse.urlencode(params), headers))


def from_tmdb(folder, kind='tv'):
    if not db.get_settings()['posters']['tmdb_key'].strip():
        return None
    title, year = clean_title(folder)
    order = (('tv', 'first_air_date_year'), ('movie', 'year'))
    for kind, yparam in (order[::-1] if kind == 'movie' else order):
        params = {'query': title, 'include_adult': 'false'}
        if year:
            params[yparam] = year
        results = _tmdb('/search/' + kind, params).get('results') or []
        for r in results:
            if r.get('poster_path'):
                return _get('https://image.tmdb.org/t/p/w342' + r['poster_path'])
    return None


def from_omdb(folder, kind='tv'):
    key = db.get_settings()['posters']['omdb_key'].strip()
    if not key:
        return None
    title, year = clean_title(folder)
    params = {'t': title, 'apikey': key}
    if kind == 'movie':
        params['type'] = 'movie'
    if year:
        params['y'] = year
    d = json.loads(_get('https://www.omdbapi.com/?' + urllib.parse.urlencode(params)))
    url = d.get('Poster')
    return _get(url) if url and url != 'N/A' else None


def lookup(folder, kind='tv'):
    """Cherche une affiche en ligne (kind : 'tv' ou 'movie', essaye en premier). Une panne d'un service
    n'empeche pas d'essayer le suivant."""
    for fn in (from_tmdb, from_omdb):
        try:
            data = fn(folder, kind)
            if data:
                return data
        except Exception as e:
            journal.warn('posters', '%s pour "%s" : %s' % (fn.__name__, folder, e))
    return None


def test():
    cfg = db.get_settings()['posters']
    out = {}
    if cfg['tmdb_key'].strip():
        r = _tmdb('/search/tv', {'query': 'Supernatural'}).get('results') or []
        out['tmdb'] = 'ok (%d resultats)' % len(r)
    if cfg['omdb_key'].strip():
        d = json.loads(_get('https://www.omdbapi.com/?' + urllib.parse.urlencode({'t': 'Supernatural', 'apikey': cfg['omdb_key']})))
        out['omdb'] = 'ok' if d.get('Response') == 'True' else d.get('Error', 'echec')
    return out
