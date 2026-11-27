# Reencodearr

Reencodearr réencode votre bibliothèque de séries et de films en HEVC (H.265) pour gagner de la place, en répartissant
le travail sur les GPU NVIDIA de une ou plusieurs machines. Il s'intègre à Sonarr et Radarr.

## Lancer le hub (Docker)

```bash
docker run -d --name reencodearr --restart unless-stopped \
  -p 8095:8095 \
  -e PUBLIC_URL=http://IP-DU-SERVEUR:8095 \
  -v /chemin/vers/series:/series \
  -v /chemin/vers/films:/movies \
  -v /chemin/vers/reencodearr-data:/data \
  ghcr.io/romain350/reencodearr:latest
```

Ou avec Docker Compose : téléchargez [`docker-compose.yml`](docker-compose.yml) et [`.env.example`](.env.example)
(à renommer en `.env` et à remplir), puis `docker compose up -d`.

Ouvrez ensuite `http://IP-DU-SERVEUR:8095` : un assistant crée le compte administrateur et propose de configurer Sonarr,
Radarr et les clés d'affiches.

## Ajouter une machine d'encodage (agent)

Dans l'onglet **Hosts → Ajouter un host**, choisissez Linux, Windows ou Windows non persistant, puis copiez la
commande affichée sur la machine qui a le GPU. Elle installe l'agent et l'enregistre auprès du hub.

Pour un agent en Docker (machine Linux avec le NVIDIA Container Toolkit), utilisez
[`docker-compose.agent.yml`](docker-compose.agent.yml) avec un code d'enrôlement obtenu dans le même onglet.

## Comment ça marche

1. Le **hub** analyse vos dossiers et affiche chaque série et chaque film avec son poids et son codec.
2. Vous sélectionnez ce qu'il faut encoder ; les fichiers partent dans une file d'attente.
3. Chaque **agent** prend un fichier, l'encode sur son GPU (NVENC), vérifie le résultat et le renvoie au hub.
4. Le hub contrôle le fichier (durée, pistes, langues, gain), remplace l'original, puis peut prévenir Sonarr ou Radarr.

Audio, sous-titres et chapitres sont copiés tels quels (sauf les sous-titres dont le codec est inconnu de FFmpeg, qu'il ne sait pas écrire, et les jaquettes intégrées). L'onglet **Statistiques** montre l'espace gagné.
Plus de détails : [guide complet](docs/GUIDE.md).

## Prérequis

- **Docker** sur la machine du hub, avec accès en écriture aux dossiers des séries et des films.
- **Au moins une carte NVIDIA** (pilote installé) sur une machine agent, Linux ou Windows. Sans GPU, l'encodage
  logiciel (CPU) est possible mais très lent.
- **Sonarr et/ou Radarr** (facultatifs) : leur URL et leur **clé API** (Settings → General → Security) servent aux
  affiches et à relancer la recherche des fichiers défectueux.
- **Clé API TMDB** (facultative, gratuite sur themoviedb.org) ou **OMDb** : affiches des séries et films absents de
  Sonarr et Radarr.
- Les agents doivent pouvoir joindre le hub à l'adresse `PUBLIC_URL` (port 8095 par défaut).

## Licence

MIT, voir [LICENSE](LICENSE).
