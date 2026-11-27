# Reencodearr : guide détaillé

Conteneur Docker qui encode une bibliothèque de séries et de films en HEVC (H.265) avec les GPU de plusieurs machines.

- Tuiles avec affiche, taille, nombre d'épisodes, nombre déjà en HEVC, état `5/70` avec roue de chargement.
- Sélection de plusieurs séries, puis : scanner, encoder, annuler, supprimer les mauvais épisodes et relancer Sonarr.
- Onglet **Films** sur le même principe, une tuile par film (taille, codec, définition, durée), avec Radarr
  pour les affiches, la relance de recherche après suppression et l'arrêt du suivi une fois le film encodé.
- Hosts ajoutés par une seule commande ; choix des GPU utilisés par host.
- File d'attente avec réservation atomique : un épisode n'est jamais encodé deux fois, sur deux machines ou non.
  Chaque réservation a son jeton et son dossier de travail : si un job est repris par un autre GPU, l'ancien worker
  est arrêté et ne peut plus agir dessus.
- Dans la file : « Prioriser » passe un fichier en tête ; « Forcer » le lance tout de suite (si aucun GPU n'est libre, l'encodage le moins avancé est interrompu et remis en file juste derrière, sans tentative perdue).
- Sélection de séries ou de films : les actions (scanner, encoder, annuler, arrêter le suivi, supprimer les défectueux)
  apparaissent dans une barre en bas de l'écran.
- Onglet **Statistiques** : espace gagné, courbe cumulée, plus gros gains, estimation de ce qui reste à gagner.
- Interface en thème clair ou sombre (suit le système), utilisable sur téléphone.
- Assistant de premier lancement (compte admin, Sonarr, Radarr, TMDB/OMDb, adresse du hub), page de connexion,
  onglet Paramètres (qualité, remplacement, renommage, scan, Sonarr, Radarr, affiches, réseau et bail des agents).

## Installation

```bash
cp .env.example .env      # puis adapter les chemins, PUBLIC_URL, PUID/PGID
docker compose up -d      # docker build -t ghcr.io/romain350/reencodearr:latest . pour construire en local
```

Au premier accès à `http://HUB:8095`, un **assistant** crée le compte administrateur puis propose de configurer
Sonarr, Radarr, les clés TMDB/OMDb et l'adresse du hub (chaque connexion peut être testée, chaque étape sauf le compte
peut être passée). Tout est enregistré dans `/data/hub.db` : l'assistant ne revient plus aux démarrages suivants, et
se relance au besoin depuis **Paramètres → Assistant de configuration**. Ouvrez l'interface juste après le premier
démarrage : tant que le compte n'est pas créé, n'importe qui joignant le port peut le créer.

**Sauvegarde de la configuration** : Paramètres → *Sauvegarde de la configuration* exporte les réglages dans un
fichier JSON, par catégorie (qualité, remplacement, scan, Sonarr, Radarr, affiches, réseau, avancé). Les clés API
(Sonarr, Radarr, TMDB, OMDb) ne sont incluses que si la case est cochée ; sans elles, l'import garde les clés déjà
enregistrées. Le fichier se réimporte au même endroit ou dès la deuxième étape de l'assistant d'une nouvelle
installation (« Restaurer »). Les comptes, les hosts et l'historique ne sont pas exportés.

Compatibilité : si `ADMIN_PASSWORD` est fourni, le compte `ADMIN_USER` est créé au démarrage comme avant, et
l'assistant s'affiche après la connexion pour le reste de la configuration. Une installation existante (compte déjà
en base) ne voit pas l'assistant.

- `/series` : un sous-dossier = une série. Le conteneur doit pouvoir y **écrire** (`PUID`/`PGID`).
- `/movies` (facultatif) : un sous-dossier = un film, comme les range Radarr (`Titre (2013)/Titre (2013).mkv`) ; un
  fichier vidéo posé directement à la racine compte aussi pour un film. Même droits d'écriture que `/series`. Sans ce
  volume, l'onglet Films l'indique et reste vide. Dans un dossier de film, le plus gros fichier donne codec,
  définition et durée de la tuile ; les autres vidéos (bonus) sont encodées aussi.
- `/data` : base, affiches, fichiers encodés en transit. Prévoir de la place (2 à 5 Go par encodage en cours).
- `PUBLIC_URL` : adresse que les **agents** utilisent pour joindre le hub.
- Un seul processus (`--workers 1`) : ne pas en lancer plusieurs.

## Ajouter un host

Onglet **Hosts → Ajouter un host**, puis coller la commande affichée sur la machine Linux (root, `python3`, `curl`, pilote NVIDIA) :

```bash
curl -fsSL 'http://HUB:8095/enroll/<code à usage unique>' | sudo bash
```

Elle installe `/opt/reencodearr-agent` et un service systemd. Si `ffmpeg` n'a pas NVENC, une version statique est téléchargée
(variable `AGENT_FFMPEG_URL` du hub pour changer la source). L'agent n'ouvre aucun port : il appelle le hub.
Les GPU détectés apparaissent dans l'onglet Hosts ; cochez ceux à utiliser. Le « CPU » (libx265) est décoché par défaut.

**Host Windows** (Windows 10/11, Windows Server 2016 et plus, GPU NVIDIA avec son pilote) : dans la même fenêtre,
choisir « Windows », puis coller la commande dans un PowerShell ouvert en administrateur :

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'http://HUB:8095/enroll/<code>/windows' | iex"
```

Elle installe dans `C:\ProgramData\Reencodearr-agent` un Python embarqué (sans installation), ffmpeg avec NVENC
(`AGENT_FFMPEG_URL_WINDOWS`, `AGENT_PYTHON_URL_WINDOWS` pour changer les sources) et l'agent, puis une tâche planifiée
« Reencodearr agent » (compte SYSTEM, au démarrage) qui le fait tourner. Journal : `agent.log` dans ce dossier.

**Machine non persistante** (VM ou serveur remis à zéro à chaque démarrage) : choisir « Windows non persistant ». La
commande est alors réutilisable : la tâche planifiée la relit sur le hub à chaque démarrage, réinstalle ce qui manque et
réenregistre la machine sous le **même host** (nom, GPU cochés gardés, nouveau jeton). Les encodages que la machine avait
avant de redémarrer repartent en file sans tentative perdue. Lancez la commande une fois avant de figer l'image ; si
l'image ne garde pas la tâche planifiée, mettez la commande dans son script de démarrage. Une commande = une machine.
Elle doit servir une première fois dans les 7 jours, puis reste valable jusqu'à « Retirer ce host », qui la révoque.

**Mettre à jour un agent** : quand un host tourne avec un agent plus ancien que celui du hub, l'onglet Hosts affiche
« Mettre à jour l'agent (1.3.0 → 1.4.0) ». Un clic suffit : le host ne prend plus de nouveau fichier, ses
téléchargements et encodages en cours repartent en file (sans tentative perdue), un envoi déjà commencé va à son
terme, puis l'agent télécharge la nouvelle version depuis le hub et redémarre. Les agents antérieurs à 1.4.0 ne savent
pas le faire seuls : le hub les arrête, et en conteneur Docker les relance avec la nouvelle version. Installé comme
service systemd, un tel agent repart dans son ancienne version : l'onglet affiche alors la commande à lancer une fois
sur la machine, les mises à jour suivantes passent par le bouton.

Fonctionnement d'un encodage : l'agent télécharge la source, encode (NVENC, avec repli en décodage logiciel si le
décodage GPU échoue), décode le résultat pour vérifier, l'envoie au hub. Le hub contrôle (durée, pistes, langues,
codec, gain), copie sous un nom caché `.xxx.part`, renomme, puis traite l'original (suppression ou quarantaine).
Si un agent disparaît, son bail expire et l'épisode repart en file.

## Épisodes et films défectueux

Le **scan rapide** relit chaque fichier sans décoder : zones à zéro, conteneur Matroska cassé, contenu manquant, absence
de piste vidéo. Un épisode marqué défectueux n'est pas mis en file (sauf case « Réessayer »). **Supprimer les défectueux**
les déplace dans `.quarantaine/` (ou les supprime, selon Paramètres), puis, si Sonarr est configuré, rescanne la série
et lance `EpisodeSearch` sur les épisodes devenus manquants. Pour un film, c'est Radarr qui rescanne le film puis lance
`MoviesSearch` s'il est surveillé et n'a plus de fichier. Chaque bibliothèque a sa propre `.quarantaine/`, vidée à la
main dans Paramètres.

## Sécurité

- Mots de passe hachés (scrypt), cookie `HttpOnly` + `SameSite=Strict`, en-tête anti-CSRF, blocage après 5 échecs.
- Les agents s'authentifient par un jeton propre à chaque host ; l'enrôlement utilise un code à usage unique (1 h).
- Placez le hub derrière HTTPS (reverse proxy) si vous l'exposez hors du réseau local.

## Tests

`python tests/e2e.py` (nécessite `ffmpeg`, `ffprobe` et `libx265`) : hub + deux agents, vrais petits fichiers,
concurrence, panne d'un agent, scan, quarantaine.
`python tests/test_reservation.py` (rapide, sans ffmpeg) : un job repris par un autre GPU après expiration du bail
n'est plus modifiable par l'ancien worker.
`python tests/test_windows_enroll.py` (quelques secondes) : script Windows, commande réutilisable d'une machine non
persistante (même host, ancien jeton refusé, encodage remis en file), révocation.
`python tests/test_agent_update.py` (environ 1 min, sans ffmpeg) : bouton de mise à jour avec un agent récent, un ancien
agent en conteneur et un ancien agent en service systemd.
`python tests/test_pistes.py` (quelques secondes, ffmpeg facultatif) : sous-titres au codec inconnu et jaquettes
ignorés, pièces jointes conservées, signes de vie pendant un encodage sans progression, annulation.

## Images Docker

GitHub Actions ([.github/workflows/docker.yml](../.github/workflows/docker.yml)) publie deux images sur ghcr.io à chaque push sur `main` :

| Image | Version lue dans | Tags |
|---|---|---|
| `ghcr.io/romain350/reencodearr` (hub) | `app/config.py` (`VERSION`) | `<version>`, `latest`, `sha-<commit>` |
| `ghcr.io/romain350/reencodearr-agent` (agent GPU) | `agent/docker/VERSION` | `<version>`, `latest`, `sha-<commit>` |

Un tag Git `v1.7.0` (hub) ou `agent-v1.0.2` (agent) publie aussi ce tag d'image. Pour sortir une version : changer le
numéro dans le fichier, fusionner dans `main`. Si les paquets ghcr.io sont privés, il faut se connecter à ghcr.io pour
tirer les images (voir [deploy/truenas/README.md](../deploy/truenas/README.md#se-connecter-à-ghcrio)).

## Déploiement sur TrueNAS

Voir [deploy/truenas/README.md](../deploy/truenas/README.md) : apps TrueNAS, réglages propres à l'installation
(`/root/reencodearr.env`), mise à jour, emplacement des secrets.

## Licence

MIT, voir [LICENSE](../LICENSE).
