# Déploiement sur TrueNAS

Testé sur TrueNAS 25.10. Deux apps Docker « custom » (compose personnalisé, créées avec `midclt call -j app.create`).

| App | Image | Rôle | Port |
|---|---|---|---|
| `reencodearr` | `ghcr.io/romain350/reencodearr:<version>` | Hub : interface web, file d'attente, base SQLite | 8095 |
| `reencodearr-agent` | `ghcr.io/romain350/reencodearr-agent:<version>` | Agent d'encodage sur le GPU NVIDIA du NAS (runtime `nvidia`) | aucun |

Les images sont construites par GitHub Actions et publiées sur ghcr.io (voir le README principal).

## Réglages de l'installation

Les scripts `deploy/truenas_*.py` ne contiennent ni adresse ni chemin propres à un NAS : ils les lisent dans
`/root/reencodearr.env` (autre fichier : `REENCODEARR_ENV=/chemin`), et une variable d'environnement de même nom
l'emporte. Une fois, en root :

```bash
cp deploy/truenas/nas.env.example /root/reencodearr.env && chmod 600 /root/reencodearr.env
nano /root/reencodearr.env      # adresse du hub, dossiers des apps, bibliothèques séries/films
```

| Variable | Rôle | Défaut |
|---|---|---|
| `REENCODEARR_PUBLIC_URL` | Adresse du hub vue par les agents (`http://<ip-du-nas>:8095`) | requis |
| `REENCODEARR_BASE` | Dossier de l'app hub (données dans `<dossier>/data`) | requis |
| `REENCODEARR_SERIES` | Bibliothèque de séries → `/series` | requis |
| `REENCODEARR_MOVIES` | Bibliothèque de films → `/movies`, montée seulement si le dossier existe | vide |
| `REENCODEARR_AGENT_BASE` | Dossier de l'app agent (jeton et travail dans `<dossier>/data`) | requis pour l'agent |
| `REENCODEARR_AGENT_NAME` | Nom de l'agent dans l'onglet Hosts | `nas-gpu` |
| `REENCODEARR_HUB_URL` | Adresse du hub vue par l'agent | `REENCODEARR_PUBLIC_URL` |
| `REENCODEARR_PORT` | Port publié par le hub | `8095` |
| `REENCODEARR_USER` | `uid:gid` du hub, qui doit pouvoir écrire dans les bibliothèques | `3000:0` |
| `REENCODEARR_CRED` | Fichier (600) du mot de passe admin généré | `/root/reencodearr-identifiants.txt` |
| `REENCODEARR_REGISTRY` | Registre des images ; vide = images locales | `ghcr.io/romain350` |

Ce fichier ne contient aucun secret ; il reste hors du dépôt pour que le dépôt ne dise rien du réseau local.

## Se connecter à ghcr.io

Si les paquets ghcr.io sont privés, le NAS doit s'authentifier une fois (inutile s'ils sont publics).

1. Sur GitHub : **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token**,
   cocher uniquement **`read:packages`**, sans expiration ou avec une date de rappel.
2. Sur le NAS, en root (shell TrueNAS ou SSH) :

   ```bash
   docker login ghcr.io -u <utilisateur-github>      # coller le jeton quand le mot de passe est demandé
   docker pull ghcr.io/romain350/reencodearr:<version>
   ```

   Le jeton est enregistré dans `/root/.docker/config.json` (jamais dans Git). Les scripts `deploy/truenas_*.py`
   font le `docker pull` eux-mêmes, en root, puis l'app démarre sur l'image déjà présente (`pull_policy: missing`).

Variante sans jeton : rendre les deux paquets publics (GitHub → profil → **Packages** → paquet → **Package settings →
Change visibility**). N'importe qui peut alors tirer l'image.

## Emplacements sur le NAS

- `<REENCODEARR_BASE>/src/` : copie de ce dépôt (sans `tests/`), qui ne sert qu'aux scripts `deploy/`.
- `<REENCODEARR_BASE>/data/` → `/data` : `hub.db` (SQLite, réglages dont les clés Sonarr/Radarr/TMDB/OMDb), `posters/`, `staging/`.
- `<REENCODEARR_SERIES>/` → `/series` : la bibliothèque, un dossier par série. Le hub tourne en `REENCODEARR_USER` et doit pouvoir y écrire.
- `<REENCODEARR_MOVIES>/` → `/movies` (hub 1.9.0) : les films, un dossier par film, mêmes droits que les séries.
- `<REENCODEARR_AGENT_BASE>/data/` → `/data` de l'agent : `config.json` (URL du hub, **jeton de l'agent**), `work/`.
- `REENCODEARR_CRED` (600) : URL, utilisateur `admin` et mot de passe admin du hub.
- `/mnt/.ix-apps/app_configs/<app>/versions/1.0.0/templates/rendered/docker-compose.yaml` : compose rendu par TrueNAS.

Les composes générés sont décrits dans ce dossier (`*.compose.yaml`), avec des repères `<...>` à la place des valeurs locales.

## Première installation

Après le `docker login` éventuel et la création de `/root/reencodearr.env` :

```bash
cd <REENCODEARR_BASE>/src                          # copie à jour de ce dépôt
python3 deploy/truenas_create_app.py <version>     # crée le hub et génère le mot de passe admin (garde l'existant)
python3 deploy/truenas_agent_app.py <version>      # crée l'agent et l'enrôle (garde le jeton existant)
```

## Mettre à jour le hub

1. Changer `VERSION` dans `app/config.py` (par exemple `1.7.0`), fusionner dans `main`.
2. Attendre la fin de l'action **Images Docker** (onglet Actions de GitHub).
3. Sur le NAS :

```bash
python3 deploy/truenas_create_app.py 1.7.0     # docker pull, puis met à jour l'app ; garde le mot de passe existant
```

## Mettre à jour l'agent

L'agent télécharge `agent.py` depuis le hub à chaque démarrage : une nouvelle version du hub suffit le plus souvent
(`docker restart reencodearr-agent`). Il ne faut une nouvelle image que si `agent/docker/` change : augmenter
`agent/docker/VERSION` (par exemple `1.0.2`), fusionner dans `main`, attendre l'action, puis :

```bash
python3 deploy/truenas_agent_app.py 1.0.2      # docker pull, puis met à jour l'app ; le jeton dans /data est conservé
```

## Construire en local (secours)

Si GitHub n'est pas joignable : `docker build -t reencodearr:1.7.0 .` puis
`REENCODEARR_REGISTRY= python3 deploy/truenas_create_app.py 1.7.0` (même principe pour l'agent avec `agent/docker`).

## Secrets (jamais dans Git)

- Mot de passe admin du hub : `REENCODEARR_CRED` et variable `ADMIN_PASSWORD` du compose TrueNAS.
- Jeton de l'agent : `<REENCODEARR_AGENT_BASE>/data/config.json`.
- Clés Sonarr / Radarr / TMDB / OMDb : dans `hub.db`, saisies dans l'assistant de premier lancement ou l'onglet Paramètres.
- Jeton GitHub `read:packages` du NAS : `/root/.docker/config.json` (écrit par `docker login`).
- La publication sur ghcr.io utilise le `GITHUB_TOKEN` fourni par Actions : aucun secret à ajouter au dépôt.
