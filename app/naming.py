"""Règle de renommage après passage en HEVC (activable dans les paramètres)."""
import os
import re

CODEC = re.compile(r'(?<=[.\-_ ])(?:H\.?264|x264|AVC|AV1)(?=[.\-_ ]|$)', re.I)
TAG = re.compile(r'-([^-.\s]+)$')
PAD3 = re.compile(r'(S\d{2}E)0(\d{2})(?!\d)')
NOT_A_TAG = {'WEB', 'WEBDL', 'WEBRIP', 'HDTV', 'BLURAY', 'DL', 'RIP', 'REMUX', 'HDR', 'DV'}


def new_name(name, opts):
    """Nom du fichier encodé (toujours en .mkv). Sans option active, seul le conteneur change."""
    base, _ = os.path.splitext(name)
    if opts.get('enabled', True):
        base = CODEC.sub('HEVC', base, count=1)
        if opts.get('pad3'):
            base = PAD3.sub(r'\1\2', base, count=1)
        if opts.get('strip_tag', True):
            m = TAG.search(base)
            if m and m.group(1).upper() not in NOT_A_TAG and not m.group(1).isdigit():
                base = base[:m.start()]
    return base + '.mkv'
