"""Mise a jour d'un agent depuis l'onglet Hosts.

L'agent (agent/agent.py) est servi par le hub : « mettre a jour » veut dire faire tourner sur la machine
la version que le hub sert. Le hub note la demande dans hosts.agent_update (JSON), puis au heartbeat suivant :
  - un agent >= 1.4.0 (info.self_update) recoit {'update': true} : il finit ses envois, telecharge agent.py,
    remplace son fichier et redemarre tout seul ;
  - un agent plus ancien ne sait pas le faire : le hub lui repond 401 une seule fois, l'agent s'arrete.
    En conteneur, Docker le relance et l'entrypoint telecharge agent.py depuis le hub : il repart a jour.
    Installe en service systemd, il repart avec le meme fichier : le hub l'indique avec la commande a lancer.
Pendant la mise a jour, le host ne recoit plus de job ; ses encodages en cours sont remis en file sans
compter de tentative (jobs.cancel = 3).
"""
import json
import os
import re
import shlex
import time

from . import db, jobs, journal

AGENT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent', 'agent.py')
TIMEOUT = 600          # sans nouvelle version au bout de 10 min, la mise a jour est consideree en echec
_latest = {'mtime': None, 'version': None}


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split('.'))
    except ValueError:
        return ()


def latest():
    """Version de l'agent servi par le hub (lue dans agent/agent.py)."""
    try:
        m = os.path.getmtime(AGENT_FILE)
    except OSError:
        return None
    if _latest['mtime'] != m:
        with open(AGENT_FILE, encoding='utf-8') as f:
            found = re.search(r"^VERSION = '([^']+)'", f.read(), re.M)
        _latest.update(mtime=m, version=found.group(1) if found else None)
    return _latest['version']


def state(host):
    try:
        return json.loads(host['agent_update'] or 'null')
    except (TypeError, ValueError):
        return None


def _save(host_id, st):
    db.run('UPDATE hosts SET agent_update=? WHERE id=?', (json.dumps(st) if st else None, host_id))


def pending(host):
    st = state(host)
    return bool(st) and not st.get('error') and time.time() - st['ts'] <= TIMEOUT


def request(host, user):
    """Demande la mise a jour du host. Les telechargements et encodages en cours s'arretent et repartent en file ;
    un envoi de resultat ou un controle final deja commence va a son terme."""
    cur = json.loads(host['info'] or '{}').get('version')
    to = latest()
    _save(host['id'], {'ts': time.time(), 'from': cur, 'to': to, 'kicked': False})
    where = ("host_id=? AND status IN ('claimed','encoding') AND cancel=0 AND phase!='check'", (host['id'],))
    n = db.one('SELECT COUNT(*) n FROM jobs WHERE ' + where[0], where[1])['n']
    db.run('UPDATE jobs SET cancel=3 WHERE ' + where[0], where[1])
    journal.info('agent', '%s demande la mise a jour de %s (agent %s -> %s)%s' % (
        user, host['name'], cur or '?', to or '?', ', %d encodage(s) remis en file' % n if n else ''))
    return {'from': cur, 'to': to}


def _orphans(host, before):
    """Jobs restes au nom de l'ancien processus de l'agent : remis en file sans penalite."""
    for j in db.q("SELECT id FROM jobs WHERE host_id=? AND status IN %s AND started<=?" % jobs.RUNNING_SQL,
                  (host['id'], before)):
        db.run('UPDATE jobs SET cancel=3 WHERE id=?', (j['id'],))
        jobs.fail(j['id'], host['id'], 'mise a jour de l agent')


def on_heartbeat(host, info):
    """Suite a donner a un heartbeat : 'update' (l'agent se met a jour), 'kick' (repondre 401 : ancien agent)
    ou None."""
    st = state(host)
    if not st or st.get('error'):
        return None
    ver = info.get('version')
    if ver != st.get('from'):
        _save(host['id'], None)
        _orphans(host, st['ts'])
        journal.info('agent', '%s : agent mis a jour (%s -> %s)' % (host['name'], st.get('from') or '?', ver or '?'))
        return None
    if time.time() - st['ts'] > TIMEOUT:
        st['error'] = "pas de nouvelle version au bout de %d minutes ; l'agent n'a pas pu se mettre a jour" % (
            TIMEOUT // 60)
        _save(host['id'], st)
        journal.error('agent', '%s : mise a jour de l agent en echec : %s' % (host['name'], st['error']))
        return None
    if info.get('self_update'):
        return 'update'
    if not st.get('kicked'):
        st['kicked'] = True
        _save(host['id'], st)
        journal.info('agent', '%s : agent %s, arret demande pour qu il redemarre avec la nouvelle version' % (
            host['name'], ver or '?'))
        return 'kick'
    # ancien agent arrete puis relance avec le meme fichier : installation systemd (hors conteneur)
    _orphans(host, st['ts'])
    st['error'] = 'restart'
    _save(host['id'], st)
    journal.warn('agent', '%s : l agent a redemarre mais est reste en %s (installation hors conteneur)' % (
        host['name'], ver or '?'))
    return None


def view(host, info, hub_url):
    """Etat affiche dans l'onglet Hosts."""
    st = state(host)
    cur, to = info.get('version'), latest()
    out = {'latest': to, 'outdated': bool(cur and to and vtuple(cur) < vtuple(to)),
           'updating': bool(st) and not st.get('error'), 'error': None}
    if st and not st.get('error') and time.time() - st['ts'] > TIMEOUT:      # agent arrete et jamais revenu
        st = dict(st, error="l'agent ne s'est pas reconnecté depuis la demande ; vérifiez qu'il tourne sur la machine")
        out['updating'] = False
    if st and st.get('error'):
        if st['error'] == 'restart':
            out['error'] = ("L'agent a redémarré mais est resté en %s : il est installé comme service (hors Docker) "
                            "et cette version ne sait pas se mettre à jour seule. Lancez une fois sur la machine la "
                            "commande ci-dessous ; les mises à jour suivantes se feront depuis ce bouton." % (cur or '?'))
            out['command'] = ('sudo curl -fsSL %s -o /opt/reencodearr-agent/agent.py '
                              '&& sudo systemctl restart reencodearr-agent' % shlex.quote(hub_url + '/agent/agent.py'))
        else:
            out['error'] = st['error']
    return out
