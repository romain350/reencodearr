#!/usr/bin/env python3
"""Cree ou met a jour l'app TrueNAS « reencodearr-agent » : un agent d'encodage qui utilise le GPU NVIDIA du NAS.

A lancer sur le NAS, en root. Usage : truenas_agent_app.py [version]
Par defaut l'image est tiree de ghcr.io/romain350/reencodearr-agent:<version> (publiee par GitHub Actions ;
le NAS doit etre connecte a ghcr.io, voir deploy/truenas/README.md).
Avec REENCODEARR_REGISTRY= (vide), utilise une image locale construite par `docker build -t reencodearr-agent:<version> agent/docker`.
Adresse du hub et chemins : fichier /root/reencodearr.env (modele deploy/truenas/nas.env.example), voir nas_config.py.
A la creation, le code d'enrolement est genere ici meme (connexion admin au hub avec le fichier
REENCODEARR_CRED, defaut /root/reencodearr-identifiants.txt) et n'apparait ni sur une ligne de commande ni dans la sortie.
A la mise a jour, le jeton de l'agent (dans le volume /data) est conserve : aucun nouveau code.
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.request

import nas_config as cfg

APP = 'reencodearr-agent'
HUB = cfg.get('REENCODEARR_HUB_URL', '') or cfg.get('REENCODEARR_PUBLIC_URL')
HUB = HUB.rstrip('/')
BASE = cfg.get('REENCODEARR_AGENT_BASE')
NAME = cfg.get('REENCODEARR_AGENT_NAME', 'nas-gpu')
CRED = cfg.get('REENCODEARR_CRED', '/root/reencodearr-identifiants.txt')
VERSION = sys.argv[1] if len(sys.argv) > 1 else '1.0.1'
REGISTRY = cfg.get('REENCODEARR_REGISTRY', 'ghcr.io/romain350')
IMAGE = (REGISTRY.rstrip('/') + '/' if REGISTRY else '') + 'reencodearr-agent:' + VERSION

if REGISTRY:
    if subprocess.run(['docker', 'pull', IMAGE]).returncode:
        sys.exit('docker pull %s a echoue : version publiee ? NAS connecte a ghcr.io (docker login) ?' % IMAGE)
elif subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True).returncode:
    sys.exit('image %s introuvable : lancer docker build avant' % IMAGE)

# GPU a utiliser : le premier NVIDIA du NAS
gpu = subprocess.run(['nvidia-smi', '--query-gpu=uuid', '--format=csv,noheader'], capture_output=True, text=True
                     ).stdout.strip().splitlines()[0]

query = subprocess.run(['midclt', 'call', 'app.query', json.dumps([['name', '=', APP]])], capture_output=True, text=True)
exists = query.stdout.strip() not in ('', '[]')

enroll_line = ''
if not exists:
    pw = [ln.split(': ', 1)[1].strip() for ln in open(CRED)
          if ln.startswith('Mot de passe')][0]
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    H = {'X-Requested-With': 'reencode', 'Content-Type': 'application/json'}
    op.open(urllib.request.Request(HUB + '/api/login', json.dumps({'username': 'admin', 'password': pw}).encode(), H)).read()
    cmd = json.loads(op.open(urllib.request.Request(HUB + '/api/hosts/enroll', b'{}', H)).read())['command']
    code = cmd.split('/enroll/')[1].split("'")[0].split(' ')[0]
    enroll_line = '      ENROLL_CODE: "%s"\n' % code

subprocess.run(['mkdir', '-p', BASE + '/data'], check=True)
compose = """services:
  reencodearr-agent:
    image: %(image)s
    pull_policy: %(pull)s
    container_name: reencodearr-agent
    restart: unless-stopped
    runtime: nvidia
    environment:
      HUB_URL: "%(hub)s"
%(enroll)s      AGENT_NAME: "%(name)s"
      NVIDIA_VISIBLE_DEVICES: "%(gpu)s"
      NVIDIA_DRIVER_CAPABILITIES: "compute,video,utility"
    volumes:
      - "%(base)s/data:/data"
""" % {'image': IMAGE, 'pull': 'missing' if REGISTRY else 'never', 'hub': HUB, 'enroll': enroll_line, 'name': NAME, 'gpu': gpu, 'base': BASE}

if exists:
    r = subprocess.run(['midclt', 'call', '-j', 'app.update', APP, json.dumps({'custom_compose_config_string': compose})],
                       capture_output=True, text=True)
else:
    payload = {'custom_app': True, 'app_name': APP, 'custom_compose_config_string': compose}
    r = subprocess.run(['midclt', 'call', '-j', 'app.create', json.dumps(payload)], capture_output=True, text=True)
print((r.stdout or '')[-300:], (r.stderr or '')[-300:])
sys.exit(r.returncode)
