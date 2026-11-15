"""Reglages propres a une installation, lus hors du depot.

Les scripts deploy/truenas_*.py lisent leurs valeurs (adresse du hub, chemins sur le NAS...) dans un fichier
KEY=VALUE, par defaut /root/reencodearr.env (autre fichier : REENCODEARR_ENV=/chemin). Une variable d'environnement
de meme nom l'emporte sur le fichier. Modele commente : deploy/truenas/nas.env.example.
"""
import os
import sys

ENV_FILE = os.environ.get('REENCODEARR_ENV', '/root/reencodearr.env')
_FILE = {}
if os.path.exists(ENV_FILE):
    for _ln in open(ENV_FILE):
        _ln = _ln.strip()
        if _ln and not _ln.startswith('#') and '=' in _ln:
            _k, _v = _ln.split('=', 1)
            _FILE[_k.strip()] = _v.strip().strip('"').strip("'")


def get(key, default=None):
    """Valeur de `key` (environnement, puis fichier, puis `default`). Sans valeur ni defaut : arret avec un message."""
    if key in os.environ:
        return os.environ[key]
    if key in _FILE:
        return _FILE[key]
    if default is None:
        sys.exit('%s manquant : le definir dans %s (modele : deploy/truenas/nas.env.example) ou dans l\'environnement'
                 % (key, ENV_FILE))
    return default
