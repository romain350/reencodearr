"""Export / import de la configuration du hub (fichier JSON).

Les réglages sont regroupés par catégorie, comme dans l'onglet Paramètres, pour pouvoir
n'exporter ou ne restaurer qu'une partie. Les clés API ne sont incluses que sur demande :
sans elles, le fichier peut être partagé sans risque, et l'import garde les clés déjà en place.
"""
import time

from . import config, db

FORMAT = 'reencodearr-config'
FORMAT_VERSION = 1

# categorie -> (libelle, cles de reglages concernees)
SECTIONS = {
    'quality': ("Qualité d'encodage", ('quality',)),
    'replace': ('Remplacement et renommage', ('replace_mode', 'rename', 'tolerance_s', 'min_gain_pct', 'av1', 'max_attempts')),
    'scan': ('Scan et épisodes défectueux', ('scan', 'bad_delete_mode')),
    'sonarr': ('Sonarr', ('sonarr',)),
    'radarr': ('Radarr', ('radarr',)),
    'posters': ('Affiches (TMDB / OMDb)', ('posters',)),
    'network': ('Réseau (URL publique du hub)', ('public_url',)),
    'advanced': ('Avancé (bail des agents)', ('lease_s',)),
}

# (cle de reglage, champ secret)
SECRETS = (('sonarr', 'api_key'), ('radarr', 'api_key'), ('posters', 'tmdb_key'), ('posters', 'omdb_key'))


def sections_info():
    return [{'id': k, 'label': v[0], 'secret': any(s[0] in v[1] for s in SECRETS)} for k, v in SECTIONS.items()]


def export(sections=None, include_secrets=False):
    sections = [s for s in (sections or SECTIONS) if s in SECTIONS]
    cur = db.get_settings()
    out = {}
    for sec in sections:
        part = {}
        for key in SECTIONS[sec][1]:
            v = cur[key]
            if isinstance(v, dict):
                v = dict(v)
                if not include_secrets:
                    for k, f in SECRETS:
                        if k == key:
                            v.pop(f, None)
            part[key] = v
        out[sec] = part
    return {'format': FORMAT, 'format_version': FORMAT_VERSION, 'hub_version': config.VERSION,
            'exported_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'secrets_included': bool(include_secrets),
            'sections': out}


def parse(data):
    """Vérifie un fichier exporté ; renvoie {categorie: {cle: valeur}} (cles inconnues ignorees)."""
    if not isinstance(data, dict) or data.get('format') != FORMAT:
        raise ValueError("ce fichier n'est pas un export de configuration Reencodearr")
    if not isinstance(data.get('format_version'), int) or data['format_version'] > FORMAT_VERSION:
        raise ValueError("format d'export trop récent : mettez d'abord le hub à jour")
    secs = data.get('sections')
    if not isinstance(secs, dict):
        raise ValueError('fichier incomplet (aucune catégorie)')
    out = {}
    for sec, part in secs.items():
        if sec not in SECTIONS or not isinstance(part, dict):
            continue
        clean = {k: v for k, v in part.items() if k in SECTIONS[sec][1]}
        if clean:
            out[sec] = clean
    if not out:
        raise ValueError('aucune catégorie reconnue dans ce fichier')
    return out


def has_secrets(part):
    return any(isinstance(part.get(k), dict) and part[k].get(f) for k, f in SECRETS)
