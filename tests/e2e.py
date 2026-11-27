"""Test de bout en bout en local : hub + deux agents CPU, vraie bibliotheque de petits fichiers.

Lancer depuis le dossier du projet : python tests/e2e.py
Necessite ffmpeg/ffprobe dans le PATH (libx265 pour le mode CPU des agents).
"""
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18095
URL = 'http://127.0.0.1:%d' % PORT
TMP = tempfile.mkdtemp(prefix='rh-e2e-')
LIB, DATA, FILMS = os.path.join(TMP, 'series'), os.path.join(TMP, 'data'), os.path.join(TMP, 'films')
procs = []
agents = {}
ok_count = 0


def check(cond, msg):
    global ok_count
    if not cond:
        print('  ECHEC :', msg)
        cleanup()
        sys.exit(1)
    ok_count += 1
    print('  ok  :', msg)


def cleanup():
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    time.sleep(1)
    shutil.rmtree(TMP, ignore_errors=True)


def ff(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y'] + list(args), check=True)


def make_video(path, seconds=6, codec='libx264', size='320x240'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ff('-f', 'lavfi', '-i', 'testsrc2=size=%s:rate=25' % size, '-f', 'lavfi', '-i', 'sine=frequency=440',
       '-f', 'lavfi', '-i', 'sine=frequency=660', '-t', str(seconds), '-map', '0:v', '-map', '1:a', '-map', '2:a',
       '-c:v', codec, '-c:a', 'aac', '-metadata:s:a:0', 'language=fre', '-metadata:s:a:1', 'language=eng', path)


# ------------------------------------------------------------------ client HTTP avec cookie
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def api(method, path, body=None, expect=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={'X-Requested-With': 'reencode', 'Content-Type': 'application/json'})
    try:
        with opener.open(req, timeout=60) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    if code != expect:
        raise AssertionError('%s %s -> %d (attendu %d) %s' % (method, path, code, expect, raw[:200]))
    return json.loads(raw) if raw and raw[:1] in b'{[' else raw


def wait(fn, timeout=120, step=1.0):
    t = time.time()
    while time.time() - t < timeout:
        r = fn()
        if r:
            return r
        time.sleep(step)
    return None


def series_by(name):
    return next((s for s in api('GET', '/api/series')['series'] if s['name'] == name), None)


def movie_by(name):
    return next((s for s in api('GET', '/api/movies')['movies'] if s['name'] == name), None)


def main():
    print('Bibliotheque de test :', LIB)
    make_video(os.path.join(FILMS, 'FilmA (2020)', 'FilmA.2020.FRENCH.1080p.WEB.H264-GRP.mkv'), seconds=8)
    make_video(os.path.join(FILMS, 'FilmB.2019.WEB.H264.mkv'), seconds=6)            # film pose a la racine
    make_video(os.path.join(FILMS, 'FilmC (2021)', 'FilmC.2021.WEB.HEVC.mkv'), codec='libx265')
    make_video(os.path.join(FILMS, 'ShowA', 'ShowA.2022.WEB.H264.mkv'))               # film du meme nom qu'une serie
    badf = os.path.join(FILMS, 'FilmD (2022)', 'FilmD.2022.WEB.H264-GRP.mkv')
    os.makedirs(os.path.dirname(badf), exist_ok=True)
    ff('-f', 'lavfi', '-i', 'nullsrc=size=640x360:rate=25,geq=random(1)*255:128:128', '-f', 'lavfi', '-i', 'sine=frequency=440',
       '-t', '8', '-c:v', 'libx264', '-b:v', '4M', '-c:a', 'aac', '-metadata:s:a:0', 'language=fre', badf)
    with open(badf, 'r+b') as f:
        f.seek(os.path.getsize(badf) // 2)
        f.write(b'\x00' * 60000)
    ff('-f', 'lavfi', '-i', 'color=c=blue:s=200x300', '-frames:v', '1', os.path.join(FILMS, 'FilmC (2021)', 'poster.jpg'))
    for i in (1, 2, 3):
        make_video(os.path.join(LIB, 'ShowA', 'Season 1', 'ShowA.S01E0%d.FRENCH.1080p.WEB.H264-GRP.mkv' % i))
    make_video(os.path.join(LIB, 'ShowB', 'Season 1', 'ShowB.S01E01.WEB.HEVC.mkv'), codec='libx265')
    bad = os.path.join(LIB, 'ShowC', 'Season 1', 'ShowC.S01E01.FRENCH.WEB.H264-GRP.mkv')
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    ff('-f', 'lavfi', '-i', 'nullsrc=size=640x360:rate=25,geq=random(1)*255:128:128', '-f', 'lavfi', '-i', 'sine=frequency=440',
       '-t', '8', '-c:v', 'libx264', '-b:v', '4M', '-c:a', 'aac', '-metadata:s:a:0', 'language=fre', bad)
    size = os.path.getsize(bad)
    with open(bad, 'r+b') as f:                      # zone remplie de zeros au milieu, comme les vrais fichiers casses
        f.seek(size // 2)
        f.write(b'\x00' * 60000)

    env = dict(os.environ, SERIES_DIR=LIB, MOVIES_DIR=FILMS, DATA_DIR=DATA, ADMIN_PASSWORD='testpass-123', PUBLIC_URL=URL)
    hub = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', str(PORT), '--workers', '1',
                            '--log-level', 'warning'], cwd=ROOT, env=env)
    procs.append(hub)
    check(wait(lambda: _up()), 'le hub demarre')

    print('Authentification')
    api('GET', '/api/series', expect=401)
    try:
        api('POST', '/api/login', {'username': 'admin', 'password': 'mauvais'}, expect=401)
        check(True, 'mauvais mot de passe refuse')
    except AssertionError as e:
        check(False, str(e))
    api('POST', '/api/login', {'username': 'admin', 'password': 'testpass-123'})
    check(api('GET', '/api/me')['user'] == 'admin', 'connexion reussie, session par cookie')

    print('Inventaire')
    check(wait(lambda: (series_by('ShowA') or {}).get('episodes') == 3 and series_by('ShowA')['pending_probe'] == 0),
          'ShowA : 3 episodes sondes')
    a, b = series_by('ShowA'), series_by('ShowB')
    check(a['hevc'] == 0 and b['hevc'] == 1 and b['episodes'] == 1, 'comptage HEVC correct (0/3 et 1/1)')
    check(a['size'] > 0, 'taille remontee')

    print('Films')
    check(wait(lambda: len(api('GET', '/api/movies')['movies']) == 5 and all(m['pending_probe'] == 0 for m in api('GET', '/api/movies')['movies'])),
          '5 films inventories et sondes (dossiers et fichier a la racine)')
    fa, fb, fc = movie_by('FilmA (2020)'), movie_by('FilmB.2019.WEB.H264'), movie_by('FilmC (2021)')
    check(fa and fa['episodes'] == 1 and fa['codec'] == 'h264' and fa['height'] == 240 and fa['duration'] > 7, 'tuile film : codec, definition, duree')
    check(fb is not None and fb['episodes'] == 1, 'film pose a la racine : son propre film')
    check(fc['hevc'] == 1, 'film deja en HEVC reconnu')
    check(movie_by('ShowA')['episodes'] == 1 and series_by('ShowA')['episodes'] == 3, 'film et serie du meme nom restent separes')
    check(not any(s['name'].startswith('.') or s['name'].startswith('Film') for s in api('GET', '/api/series')['series']), 'aucun film dans l onglet Series')
    check(api('GET', '/api/movies/' + urllib.request.quote('FilmA (2020)') + '/files')['files'][0]['file'] == 'FilmA.2020.FRENCH.1080p.WEB.H264-GRP.mkv',
          'detail du fichier d un film')

    print('Affiches et cache')
    ff('-f', 'lavfi', '-i', 'color=c=red:s=200x300', '-frames:v', '1', os.path.join(LIB, 'ShowB', 'poster.jpg'))

    def poster_code(name):
        req = urllib.request.Request(URL + '/api/poster/' + name)
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, r.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            return e.code, ''
    code, ctype = poster_code('ShowB')
    check(code == 200 and 'image' in ctype, 'affiche locale (poster.jpg) servie')
    check(poster_code('ShowA')[0] == 404, 'serie sans affiche : 404 propre (la tuile garde sa lettre)')
    code, ctype = poster_code(urllib.request.quote('FilmC (2021)') + '?kind=movie')
    check(code == 200 and 'image' in ctype, 'affiche locale d un film servie')
    check(poster_code(urllib.request.quote('..') + '?kind=movie')[0] == 404, 'affiche : chemin hors bibliotheque refuse')
    check(any(f.endswith('.none') for f in os.listdir(os.path.join(DATA, 'posters'))), 'echec memorise dans le cache (pas de requete a chaque affichage)')
    check(api('DELETE', '/api/posters/cache')['removed'] >= 1, 'cache des affiches vide depuis les parametres')
    st = api('PUT', '/api/settings', {'posters': {'tmdb_key': 'cle-test-123', 'language': 'xx'}})
    p = api('GET', '/api/settings')['settings']['posters']
    check(p['tmdb_key'] == '********' and p['language'] == 'fr-FR', 'cle TMDB masquee dans l\'API, langue invalide corrigee')
    api('PUT', '/api/settings', {'posters': {'tmdb_key': '', 'language': 'fr-FR'}})

    print('Export / import de la configuration')
    api('PUT', '/api/settings', {'sonarr': {'url': 'http://sonarr.test:8989/', 'api_key': 'cle-sonarr-1'},
                                 'quality': {'cq': 24}})
    ex = api('GET', '/api/settings/export')
    check(ex['format'] == 'reencodearr-config' and not ex['secrets_included'] and 'api_key' not in ex['sections']['sonarr']['sonarr']
          and ex['sections']['sonarr']['sonarr']['url'] == 'http://sonarr.test:8989', 'export sans les cles API par defaut')
    check('setup_done' not in json.dumps(ex) and 'paused' not in json.dumps(ex), 'etat interne (assistant, pause) non exporte')
    check(ex['sections']['radarr']['radarr'].get('unmonitor_done') is True, 'reglage Radarr (fin du suivi des films) exporte')
    exs = api('GET', '/api/settings/export?sections=sonarr,quality&secrets=1')
    check(sorted(exs['sections']) == ['quality', 'sonarr'] and exs['sections']['sonarr']['sonarr']['api_key'] == 'cle-sonarr-1',
          'export partiel avec les cles sur demande')
    api('PUT', '/api/settings', {'sonarr': {'url': 'http://autre:1', 'api_key': 'cle-sonarr-2'}, 'quality': {'cq': 30}})
    dry = api('POST', '/api/settings/import', {'data': ex, 'dry_run': True})
    check(len(dry['found']) == len(ex['sections']) and api('GET', '/api/settings')['settings']['quality']['cq'] == 30,
          'apercu de l import sans rien modifier')
    r = api('POST', '/api/settings/import', {'data': ex, 'sections': ['sonarr']})
    s = api('GET', '/api/settings')['settings']
    check(r['applied'] == ['sonarr'] and s['sonarr']['url'] == 'http://sonarr.test:8989' and s['quality']['cq'] == 30,
          'import limite aux categories choisies')
    check(api('GET', '/api/settings/export?sections=sonarr&secrets=1')['sections']['sonarr']['sonarr']['api_key'] == 'cle-sonarr-2',
          'cle absente du fichier : la cle en place est gardee')
    api('POST', '/api/settings/import', {'data': exs})
    s = api('GET', '/api/settings')['settings']
    check(s['quality']['cq'] == 24 and s['quality']['preset'] == 'p6', 'import complet (qualite restauree, champs voisins intacts)')
    check(api('GET', '/api/settings/export?sections=sonarr&secrets=1')['sections']['sonarr']['sonarr']['api_key'] == 'cle-sonarr-1',
          'cle API restauree depuis un export qui la contient')
    api('POST', '/api/settings/import', {'data': {'format': 'autre'}}, expect=400)
    bad = json.loads(json.dumps(ex)); bad['sections']['quality']['quality'] = 'pas un objet'
    api('POST', '/api/settings/import', {'data': bad}, expect=400)
    check(api('GET', '/api/settings')['settings']['quality']['cq'] == 24, 'fichier invalide refuse sans toucher aux reglages')
    api('PUT', '/api/settings', {'sonarr': {'url': '', 'api_key': ''}, 'quality': {'cq': 28}})

    print('Scan des defauts')
    api('POST', '/api/actions', {'action': 'scan', 'series': ['ShowA', 'ShowC'], 'level': 'quick'})
    check(wait(lambda: series_by('ShowC')['bad'] == 1 and not series_by('ShowC')['scan']), 'ShowC : episode casse detecte')
    check(series_by('ShowA')['bad'] == 0, 'ShowA : aucun faux positif')
    eps = api('GET', '/api/series/ShowC/episodes')['episodes']
    print('     raison :', eps[0]['issues'])

    print('Reglages')
    api('PUT', '/api/settings', {'quality': {'cpu_preset': 'ultrafast', 'cpu_crf': 28}, 'min_gain_pct': 0,
                                 'rename': {'enabled': True, 'strip_tag': True, 'pad3': False}})
    check(api('GET', '/api/settings')['settings']['quality']['cpu_preset'] == 'ultrafast', 'parametres enregistres')

    print('Hosts et agents')
    for n in ('hostA', 'hostB'):
        cmd = api('POST', '/api/hosts/enroll')['command']
        code = cmd.split('/enroll/')[1].split("'")[0].split(' ')[0]
        script = urllib.request.urlopen(URL + '/enroll/' + code).read().decode()
        check('agent.py" register' in script and 'systemctl' in script, 'script d\'installation servi pour %s' % n)
        adir = os.path.join(TMP, n)
        subprocess.run([sys.executable, os.path.join(ROOT, 'agent', 'agent.py'), 'register', '--hub', URL, '--code', code,
                        '--name', n, '--ffmpeg', 'ffmpeg', '--dir', adir], check=True, capture_output=True)
        # le code est a usage unique
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'agent', 'agent.py'), 'register', '--hub', URL, '--code', code,
                            '--name', n + 'x', '--ffmpeg', 'ffmpeg', '--dir', adir + 'x'], capture_output=True, text=True)
        check(r.returncode != 0, 'code d\'enrolement refuse la 2e fois')
        agents[n] = subprocess.Popen([sys.executable, os.path.join(ROOT, 'agent', 'agent.py'), 'run', '--dir', adir])
        procs.append(agents[n])
    hosts = wait(lambda: (lambda h: h if len(h) == 2 and all(x['online'] and x['gpus'] for x in h) else None)(
        api('GET', '/api/hosts')['hosts']))
    check(hosts is not None, 'deux hosts en ligne avec leurs GPU detectes')
    for h in hosts:
        api('PATCH', '/api/hosts/%d' % h['id'], {'gpus_enabled': ['cpu']})
    check(all(x['gpus_enabled'] == ['cpu'] for x in api('GET', '/api/hosts')['hosts']), 'selection des GPU par host')

    print('Encodage en file, sans doublon')
    api('PUT', '/api/settings', {'paused': True})
    r1 = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowA']})
    r2 = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowA']})
    check(r1['enqueued'] == 3 and r2['enqueued'] == 0 and r2['already_queued'] == 3, 'double lancement : aucun doublon')
    r3 = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowB', 'ShowC']})
    check(r3['enqueued'] == 0 and r3['already_hevc'] == 1 and r3['skipped_bad'] == 1, 'HEVC et defectueux ignores')
    time.sleep(12)
    check(all(j['status'] == 'queued' for j in api('GET', '/api/queue')['jobs']), 'file en pause : rien n\'est distribue')
    api('PUT', '/api/settings', {'paused': False})
    check(wait(lambda: series_by('ShowA')['hevc'] == 3, timeout=240), 'les 3 episodes sont passes en HEVC')
    jobs = [j for j in api('GET', '/api/queue')['jobs'] if j['series'] == 'ShowA']
    check(len(jobs) == 3 and all(j['status'] == 'done' and j['attempts'] == 1 for j in jobs), 'chaque episode encode une seule fois')
    check(len({j['host'] for j in jobs}) >= 1, 'hosts utilises : %s' % sorted({j['host'] for j in jobs}))

    print('Resultat sur disque')
    files = sorted(os.listdir(os.path.join(LIB, 'ShowA', 'Season 1')))
    print('     ', files)
    check(files == ['ShowA.S01E01.FRENCH.1080p.WEB.HEVC.mkv', 'ShowA.S01E02.FRENCH.1080p.WEB.HEVC.mkv',
                    'ShowA.S01E03.FRENCH.1080p.WEB.HEVC.mkv'], 'fichiers renommes, originaux remplaces, aucun .part')
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,codec_type:stream_tags=language',
                        '-of', 'csv=p=0', os.path.join(LIB, 'ShowA', 'Season 1', files[0])], capture_output=True, text=True)
    check('hevc' in p.stdout and p.stdout.count('aac') == 2 and 'fre' in p.stdout and 'eng' in p.stdout,
          'HEVC + 2 pistes audio copiees avec leurs langues')
    check(not os.listdir(os.path.join(DATA, 'staging')), 'zone de transit videe')
    a = series_by('ShowA')
    check(a['batch']['done'] == 3 and a['batch']['total'] == 3 and a['batch']['active'] == 0, 'etat de la tuile : 3/3 termine')

    print('Encodage des films')
    r = api('POST', '/api/actions', {'action': 'encode', 'kind': 'movie', 'series': ['FilmA (2020)', 'FilmB.2019.WEB.H264', 'FilmC (2021)']})
    check(r['enqueued'] == 2 and r['already_hevc'] == 1, 'films mis en file (le film HEVC est ignore)')
    check(any(j['kind'] == 'movie' and j['series'] == 'FilmA (2020)' for j in api('GET', '/api/queue')['jobs']), 'file d attente : film affiche sous son nom')
    check(wait(lambda: (movie_by('FilmA (2020)') or {}).get('hevc') == 1 and movie_by('FilmB.2019.WEB.HEVC') is not None, timeout=240),
          'films encodes en HEVC')
    check(os.listdir(os.path.join(FILMS, 'FilmA (2020)')) == ['FilmA.2020.FRENCH.1080p.WEB.HEVC.mkv'], 'film renomme et remplace dans son dossier')
    check(sorted(f for f in os.listdir(FILMS) if f.endswith('.mkv')) == ['FilmB.2019.WEB.HEVC.mkv'], 'film a la racine remplace')
    fa = movie_by('FilmA (2020)')
    check(fa['batch']['done'] == 1 and fa['batch']['total'] == 1 and fa['codec'] == 'hevc', 'tuile film : 1/1 termine')
    check(series_by('ShowA')['hevc'] == 3 and movie_by('ShowA')['hevc'] == 0, 'film homonyme non touche par l encodage de la serie')
    api('POST', '/api/actions', {'action': 'scan', 'kind': 'movie', 'series': ['FilmD (2022)'], 'level': 'quick'})
    check(wait(lambda: movie_by('FilmD (2022)')['bad'] == 1 and not movie_by('FilmD (2022)')['scan']), 'film casse detecte par le scan')
    res = api('POST', '/api/actions', {'action': 'delete_bad', 'kind': 'movie', 'series': ['FilmD (2022)'], 'confirm': True})
    check(res['removed'] == 1 and os.path.isfile(os.path.join(FILMS, '.quarantaine', 'mauvais', 'FilmD (2022)', 'FilmD.2022.WEB.H264-GRP.mkv')),
          'film casse mis dans la quarantaine des films')
    check(movie_by('FilmD (2022)') is None, 'film retire de la liste')
    check(api('GET', '/api/settings')['quarantine']['files'] == 1, 'quarantaine des films comptee dans les parametres')
    api('POST', '/api/actions', {'action': 'unmonitor', 'kind': 'movie', 'series': ['FilmA (2020)']}, expect=400)

    print('Suppression des mauvais episodes')
    api('POST', '/api/actions', {'action': 'delete_bad', 'series': ['ShowC']}, expect=400)
    res = api('POST', '/api/actions', {'action': 'delete_bad', 'series': ['ShowC'], 'confirm': True})
    check(res['removed'] == 1 and res['mode'] == 'quarantine', 'episode casse retire en quarantaine')
    check(os.path.exists(os.path.join(LIB, '.quarantaine', 'mauvais', 'ShowC', 'Season 1', os.listdir(
        os.path.join(LIB, '.quarantaine', 'mauvais', 'ShowC', 'Season 1'))[0])), 'fichier retrouve en quarantaine')
    check(not os.listdir(os.path.join(LIB, 'ShowC', 'Season 1')), 'plus dans la bibliotheque')
    api('DELETE', '/api/quarantine', expect=400)

    print('Concurrence : deux machines, 8 episodes')
    for i in range(1, 9):
        make_video(os.path.join(LIB, 'ShowD', 'Season 1', 'ShowD.S01E0%d.WEB.H264-GRP.mkv' % i), seconds=40)
    api('PUT', '/api/settings', {'lease_s': 12})
    r = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowD']})
    check(r['enqueued'] == 8, '8 episodes en file')

    def running():
        return [j for j in api('GET', '/api/queue')['jobs'] if j['series'] == 'ShowD' and j['status'] in ('claimed', 'encoding', 'uploading')]
    check(wait(lambda: len({j['host'] for j in running()}) == 2, timeout=60, step=0.3), 'les deux hosts travaillent en meme temps')
    files_now = [j['file'] for j in running()]
    check(len(files_now) == len(set(files_now)), 'jamais le meme episode sur deux machines')
    print('Panne d un agent en plein encodage')
    agents['hostA'].kill()
    check(wait(lambda: series_by('ShowD')['hevc'] == 8, timeout=480), 'les 8 episodes finissent malgre la panne de hostA')
    done = [j for j in api('GET', '/api/queue')['jobs'] if j['series'] == 'ShowD' and j['status'] == 'done']
    check(len(done) == 8 and len({j['file'] for j in done}) == 8, '8 jobs termines, un par episode')
    print('     tentatives :', sorted(j['attempts'] for j in done), ' hosts :', sorted({j['host'] for j in done}))
    check(len(os.listdir(os.path.join(LIB, 'ShowD', 'Season 1'))) == 8, '8 fichiers HEVC, aucun doublon ni reste')

    print('Mettre en priorite et forcer un fichier')
    check(wait(lambda: sum(h['online'] for h in api('GET', '/api/hosts')['hosts']) == 1, timeout=90), 'hostA vu hors ligne : un seul GPU disponible')
    for i in range(1, 5):
        make_video(os.path.join(LIB, 'ShowG', 'Season 1', 'ShowG.S01E0%d.WEB.H264-GRP.mkv' % i), seconds=120,
                   size='1280x720')     # encodage de plusieurs secondes (machine rapide comprise) : l'interruption a le temps d'arriver
    api('POST', '/api/actions', {'action': 'scan', 'series': ['ShowG']})
    wait(lambda: series_by('ShowG') is not None and series_by('ShowG')['pending_probe'] == 0, timeout=90)
    api('PUT', '/api/settings', {'paused': True})
    check(api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowG']})['enqueued'] == 4, 'ShowG : 4 episodes en file (file en pause)')

    def g_jobs():
        return [j for j in api('GET', '/api/queue')['jobs'] if j['series'] == 'ShowG' and j['status'] not in ('canceled',)]
    queued = [j for j in g_jobs() if j['status'] == 'queued']
    last = queued[-1]
    r = api('POST', '/api/jobs/%d/prioritize' % last['id'], {})
    check(r['ok'] and r['paused'] and r['preempted'] is None, 'priorite acceptee, file signalee en pause')
    check([j for j in g_jobs() if j['status'] == 'queued'][0]['id'] == last['id'], 'le fichier prioritaire passe en tete de file')
    api('PUT', '/api/settings', {'paused': False})
    check(wait(lambda: any(j['id'] == last['id'] and j['status'] in ('claimed', 'encoding') for j in g_jobs()), timeout=30, step=0.3),
          'le fichier prioritaire est encode en premier')
    victim = last['id']
    target = [j for j in g_jobs() if j['status'] == 'queued'][-1]
    r = api('POST', '/api/jobs/%d/prioritize' % target['id'], {'force': True})
    check(r['ok'] and r['preempted'] == victim, 'forcer sans GPU libre : interrompt l encodage en cours')
    check(wait(lambda: any(j['id'] == target['id'] and j['status'] in ('claimed', 'encoding') for j in g_jobs()), timeout=60, step=0.3),
          'le fichier force est lance sur le GPU libere')
    qlog = [l['message'] for l in reversed(api('GET', '/api/logs?source=queue&limit=500')['logs'])]
    i_int = next((i for i, m in enumerate(qlog) if 'job %d interrompu pour un fichier force' % victim in m), None)
    check(i_int is not None, 'l encodage interrompu est remis en file (journal)')
    after = [m for m in qlog[i_int:] if ' reserve par ' in m]
    check(after and after[0].startswith('job %d ' % target['id']), 'le GPU libere prend le fichier force, pas celui interrompu')
    api('POST', '/api/jobs/%d/prioritize' % target['id'], {}, expect=409)
    check(wait(lambda: series_by('ShowG')['hevc'] == 4, timeout=480), 'les 4 episodes de ShowG finissent')
    done = [j for j in api('GET', '/api/queue')['jobs'] if j['series'] == 'ShowG' and j['status'] == 'done']
    check(len(done) == 4 and len({j['file'] for j in done}) == 4, '4 jobs termines, aucun doublon')
    check([j['attempts'] for j in done if j['id'] == victim] == [1], 'l interruption ne compte pas comme une tentative')

    print('Sources deja modernes ou deja compactes')
    api('PUT', '/api/settings', {'min_gain_pct': 99})          # avant la mise en file : aucun gain ne sera jamais suffisant
    make_video(os.path.join(LIB, 'ShowE', 'Season 1', 'ShowE.S01E01.WEB.H264-GRP.mkv'), seconds=6)
    try:
        make_video(os.path.join(LIB, 'ShowF', 'Season 1', 'ShowF.S01E01.WEB.AV1-GRP.mkv'), seconds=6, codec='libsvtav1')
        have_av1 = True
    except Exception:
        have_av1 = False
    api('POST', '/api/actions', {'action': 'scan', 'series': ['ShowE']})
    wait(lambda: series_by('ShowE') is not None, timeout=90)
    r = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowE']})
    check(r['enqueued'] == 1, 'ShowE mis en file')
    if have_av1:
        wait(lambda: series_by('ShowF') is not None and series_by('ShowF')['pending_probe'] == 0, timeout=90)
        r = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowF']})
        check(r['enqueued'] == 0 and r['already_modern'] == 1, 'source AV1 : non reencodee (le HEVC la ferait grossir)')
        api('PUT', '/api/settings', {'av1': {'reencode': True, 'max_growth_pct': 1000}})
        r = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowF']})
        check(r['enqueued'] == 1, 'option AV1 -> HEVC : source AV1 mise en file')
        check(wait(lambda: series_by('ShowF')['hevc'] == 1, timeout=180),
              'AV1 -> HEVC accepte malgre le gain minimal de 99 % (fichier plus gros autorise)')
        check(os.listdir(os.path.join(LIB, 'ShowF', 'Season 1')) == ['ShowF.S01E01.WEB.HEVC.mkv'], 'AV1 renomme en HEVC')
        api('PUT', '/api/settings', {'av1': {'reencode': False}})
    check(wait(lambda: any(j['series'] == 'ShowE' and j['status'] == 'skipped' for j in api('GET', '/api/queue')['jobs']), timeout=120),
          'gain insuffisant : job ignore (statut skipped), pas en erreur')
    e = series_by('ShowE')
    check(e['bad'] == 0 and e['batch']['failed'] == 0, 'episode non marque defectueux')
    check(os.path.exists(os.path.join(LIB, 'ShowE', 'Season 1', 'ShowE.S01E01.WEB.H264-GRP.mkv')), 'original conserve')
    r = api('POST', '/api/actions', {'action': 'encode', 'series': ['ShowE']})
    check(r['enqueued'] == 0 and r['skipped_compact'] == 1, 'episode deja essaye : pas remis en file en boucle')
    api('PUT', '/api/settings', {'min_gain_pct': 0})

    print('Journal et regression du bouton Encoder')
    src_js = open(os.path.join(ROOT, 'app', 'static', 'app.js'), encoding='utf-8').read()
    check('{ action,' not in src_js, 'app.js : aucun envoi de la fonction action au lieu de son nom (bug du bouton Encoder)')
    api('POST', '/api/actions', {'series': ['ShowA']}, expect=400)
    api('POST', '/api/actions', {'action': 'nimportequoi', 'series': ['ShowA']}, expect=400)
    logs = api('GET', '/api/logs?limit=2000')['logs']
    texts = ' || '.join(l['message'] for l in logs)
    check('connexion de admin' in texts and 'echec de connexion' in texts, 'journal : connexions reussies et echouees')
    check('mise en file' in texts and 'reserve par' in texts and 'job' in texts and 'OK :' in texts, 'journal : mise en file, reservation, remplacement')
    check('action inconnue recue' in texts, 'journal : action invalide tracee')
    check('nouveau host enregistre' in texts and 'agent' in {l['source'] for l in logs}, 'journal : enregistrement des hosts')
    check(any(l['level'] == 'warn' and l['source'] == 'scan' for l in logs), 'journal : episode casse signale par le scan')
    check(all(l['level'] == 'error' for l in api('GET', '/api/logs?level=error')['logs']), 'filtre par niveau')
    check(len(api('GET', '/api/logs?source=queue')['logs']) > 0 and all(l['source'] == 'queue' for l in api('GET', '/api/logs?source=queue')['logs']),
          'filtre par source')
    check(len(api('GET', '/api/logs?q=ShowD')['logs']) > 0, 'recherche texte')
    api('DELETE', '/api/logs', expect=400)
    check(api('DELETE', '/api/logs?confirm=yes')['removed'] > 10, 'vidage du journal avec confirmation')

    print('\n%d verifications reussies' % ok_count)
    cleanup()


def _up():
    try:
        urllib.request.urlopen(URL + '/', timeout=2).read()
        return True
    except Exception:
        return False


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        cleanup()
        raise
