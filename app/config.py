"""Configuration par variables d'environnement (les réglages métier sont en base)."""
import os

SERIES_DIR = os.environ.get('SERIES_DIR', '/series')
MOVIES_DIR = os.environ.get('MOVIES_DIR', '/movies')   # un sous-dossier (ou un fichier) = un film
DATA_DIR = os.environ.get('DATA_DIR', '/data')
PUBLIC_URL = os.environ.get('PUBLIC_URL', '').rstrip('/')
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('FFPROBE', 'ffprobe')
VERSION = '1.14.3'

STAGING_DIR = os.path.join(DATA_DIR, 'staging')
POSTER_DIR = os.path.join(DATA_DIR, 'posters')
QUARANTINE = '.quarantaine'          # dossier caché à la racine de la bibliothèque (ignoré par Jellyfin)
VIDEO_EXT = {'.mkv', '.mp4', '.m4v', '.avi', '.ts', '.mov'}
HEVC_CODECS = {'hevc', 'h265'}
MODERN_CODECS = {'hevc', 'h265', 'av1', 'vp9'}   # deja au moins aussi efficaces que le HEVC : on ne les reencode pas
AV1_CODECS = {'av1'}


def kept_codecs(settings):
    """Codecs laisses tels quels. L'AV1 en sort quand le passage AV1 -> HEVC est active (compatibilite)."""
    if settings.get('av1', {}).get('reencode'):
        return MODERN_CODECS - AV1_CODECS
    return MODERN_CODECS
# Les films partagent les tables des series : leur chemin (et leur cle de groupe) commence par ce prefixe.
# Un dossier cache ne peut pas etre une serie (ils sont ignores), donc aucune collision n'est possible.
MOVIE_PREFIX = '.films/'
ACTIVE = ('queued', 'claimed', 'encoding', 'uploading', 'verifying')
