#!/usr/bin/env python3
"""Cree l'app TrueNAS « reencodearr » (compose personnalise) avec un mot de passe admin genere.

A lancer sur le NAS, en root. Usage : truenas_create_app.py [version]
Par defaut l'image est tiree de ghcr.io/romain350/reencodearr:<version> (publiee par GitHub Actions ;
le NAS doit etre connecte a ghcr.io, voir deploy/truenas/README.md).
Avec REENCODEARR_REGISTRY= (vide), utilise une image locale reencodearr:<version> construite par `docker build`.
Adresse du hub et chemins : fichier /root/reencodearr.env (modele deploy/truenas/nas.env.example), voir nas_config.py.
Le mot de passe est ecrit dans REENCODEARR_CRED (defaut /root/reencodearr-identifiants.txt, lecture root seulement).
"""
import json
import os
import secrets
import subprocess
import sys

import nas_config as cfg

VERSION = sys.argv[1] if len(sys.argv) > 1 else '1.6.0'
REGISTRY = cfg.get('REENCODEARR_REGISTRY', 'ghcr.io/romain350')
IMAGE = (REGISTRY.rstrip('/') + '/' if REGISTRY else '') + 'reencodearr:' + VERSION

APP = 'reencodearr'
BASE = cfg.get('REENCODEARR_BASE')
SERIES = cfg.get('REENCODEARR_SERIES')
# Films (onglet Films, depuis 1.9.0) : monte seulement si le dossier existe sur le NAS.
MOVIES = cfg.get('REENCODEARR_MOVIES', '')
PORT = int(cfg.get('REENCODEARR_PORT', '8095'))
URL = cfg.get('REENCODEARR_PUBLIC_URL').rstrip('/')
USER = cfg.get('REENCODEARR_USER', '3000:0')
CRED = cfg.get('REENCODEARR_CRED', '/root/reencodearr-identifiants.txt')

if REGISTRY:
    if subprocess.run(['docker', 'pull', IMAGE]).returncode:
        sys.exit('docker pull %s a echoue : version publiee ? NAS connecte a ghcr.io (docker login) ?' % IMAGE)
elif subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True).returncode:
    sys.exit('image %s introuvable : lancer docker build avant' % IMAGE)

password = None
if os.path.exists(CRED):                      # conserve le mot de passe existant (la base de donnees le garde de toute facon)
    for line in open(CRED):
        if line.startswith('Mot de passe : '):
            password = line.split(': ', 1)[1].strip()
password = password or secrets.token_urlsafe(14)
if not (MOVIES and os.path.isdir(MOVIES)):
    print('dossier des films %r absent : onglet Films vide (REENCODEARR_MOVIES=... pour un autre chemin)' % MOVIES)
compose = """services:
  reencodearr:
    image: %(image)s
    pull_policy: %(pull)s
    container_name: reencodearr
    restart: unless-stopped
    user: "%(user)s"
    ports:
      - "%(port)d:8095"
    environment:
      PUBLIC_URL: "%(url)s"
      ADMIN_USER: "admin"
      ADMIN_PASSWORD: "%(pw)s"
    volumes:
      - "%(series)s:/series"
%(movies)s      - "%(base)s/data:/data"
""" % {'movies': '      - "%s:/movies"\n' % MOVIES if MOVIES and os.path.isdir(MOVIES) else '', 'user': USER, 'image': IMAGE, 'pull': 'missing' if REGISTRY else 'never', 'port': PORT, 'url': URL, 'pw': password, 'series': SERIES, 'base': BASE}

fd = os.open(CRED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('URL : %s\nUtilisateur : admin\nMot de passe : %s\n' % (URL, password))

exists = subprocess.run(['midclt', 'call', 'app.query', json.dumps([['name', '=', APP]])], capture_output=True, text=True).stdout.strip() not in ('', '[]')
if exists:      # mise a jour : meme app, nouvelle image
    r = subprocess.run(['midclt', 'call', '-j', 'app.update', APP, json.dumps({'custom_compose_config_string': compose})],
                       capture_output=True, text=True)
else:
    payload = {'custom_app': True, 'app_name': APP, 'custom_compose_config_string': compose}
    r = subprocess.run(['midclt', 'call', '-j', 'app.create', json.dumps(payload)], capture_output=True, text=True)
print(r.stdout[-600:] or '', r.stderr[-600:] or '')
sys.exit(r.returncode)
