"""Journal d'activite : ecrit en base (consultable dans l'onglet Journal) et sur la sortie du conteneur."""
import time

from . import db

KEEP = 5000
LEVELS = {'info': 0, 'warn': 1, 'error': 2}
_count = [0]


def log(level, source, message):
    """level : info | warn | error. source : auth, action, queue, agent, scan, replace, sonarr, posters, system."""
    message = str(message)[:1000]
    print('[%s] %-5s %-8s %s' % (time.strftime('%d/%m %H:%M:%S'), level.upper(), source, message), flush=True)
    try:
        db.run('INSERT INTO logs(ts,level,source,message) VALUES(?,?,?,?)', (time.time(), level, source, message))
        _count[0] += 1
        if _count[0] % 200 == 0:
            db.run('DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?', (KEEP,))
    except Exception as e:                         # le journal ne doit jamais casser l'application
        print('[journal] ecriture impossible :', e, flush=True)


def info(source, message):
    log('info', source, message)


def warn(source, message):
    log('warn', source, message)


def error(source, message):
    log('error', source, message)


def query(limit=300, level=None, source=None, text=None, before=None):
    sql, args = 'SELECT id,ts,level,source,message FROM logs WHERE 1=1', []
    if level in LEVELS and level != 'info':          # warn -> warn+error ; error -> error seulement
        sql += ' AND level IN (%s)' % ','.join('?' * len([k for k, v in LEVELS.items() if v >= LEVELS[level]]))
        args += [k for k, v in LEVELS.items() if v >= LEVELS[level]]
    if source:
        sql += ' AND source=?'
        args.append(source)
    if text:
        sql += ' AND message LIKE ?'
        args.append('%' + text + '%')
    if before:
        sql += ' AND id<?'
        args.append(int(before))
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(max(1, min(int(limit), 2000)))
    return [dict(r) for r in db.q(sql, args)]


def clear():
    n = db.one('SELECT COUNT(*) c FROM logs')['c']
    db.run('DELETE FROM logs')
    return n
