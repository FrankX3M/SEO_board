"""Site-scoped contacts and page-type assignments; legacy owners remain intact."""
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_contacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
 email TEXT NOT NULL, area TEXT NOT NULL DEFAULT '', UNIQUE(site,email,area)
);
CREATE TABLE IF NOT EXISTS site_type_contacts (
 site TEXT NOT NULL, page_type TEXT NOT NULL, contact_id INTEGER NOT NULL,
 coverage TEXT NOT NULL DEFAULT '', PRIMARY KEY(site,page_type,contact_id)
);
CREATE TABLE IF NOT EXISTS site_contacts_migrations (site TEXT PRIMARY KEY);
"""


def migrate_site(db, site):
    from .webdb import tx
    with tx(db):
        if db.execute('SELECT 1 FROM site_contacts_migrations WHERE site=?', (site,)).fetchone():
            return
        for row in db.execute('SELECT * FROM ref_owners').fetchall():
            db.execute('INSERT OR IGNORE INTO site_contacts(site,email,area) VALUES (?,?,?)',
                       (site, row['email'], row['area']))
            contact = db.execute('SELECT id FROM site_contacts WHERE site=? AND email=? AND area=?',
                                 (site, row['email'], row['area'])).fetchone()[0]
            db.execute('INSERT OR IGNORE INTO site_type_contacts VALUES (?,?,?,?)',
                       (site, row['page_type'], contact, row['coverage']))
        db.execute('INSERT INTO site_contacts_migrations VALUES (?)', (site,))


def contacts(db, site):
    return [dict(r) for r in db.execute('SELECT * FROM site_contacts WHERE site=? ORDER BY name,email,area', (site,))]


def assignments(db, site):
    out = {}
    for row in db.execute('SELECT page_type,contact_id FROM site_type_contacts WHERE site=?', (site,)):
        out.setdefault(row[0], []).append(row[1])
    return out


def save(db, site, form, contact_id=None):
    import sqlite3
    name, email, area = (str(form.get(k, '')).strip() for k in ('name', 'email', 'area'))
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or max(len(name),len(email),len(area)) > 254:
        raise ValueError('Укажите корректную почту; поля должны быть не длиннее 254 символов')
    if contact_id is not None and not db.execute('SELECT 1 FROM site_contacts WHERE id=? AND site=?', (contact_id,site)).fetchone():
        raise ValueError('Ответственный не найден на этом сайте')
    try:
        if contact_id is None:
            return db.execute('INSERT INTO site_contacts(site,name,email,area) VALUES (?,?,?,?)', (site,name,email,area)).lastrowid
        db.execute('UPDATE site_contacts SET name=?,email=?,area=? WHERE id=? AND site=?', (name,email,area,contact_id,site))
        return contact_id
    except sqlite3.IntegrityError:
        raise ValueError('Ответственный с такой почтой и областью уже существует') from None


def delete(db, site, contact_id):
    if not db.execute('SELECT 1 FROM site_contacts WHERE id=? AND site=?', (contact_id,site)).fetchone():
        raise ValueError('Ответственный не найден на этом сайте')
    db.execute('DELETE FROM site_type_contacts WHERE site=? AND contact_id=?', (site,contact_id))
    db.execute('DELETE FROM site_contacts WHERE site=? AND id=?', (site,contact_id))


def assign(db, site, page_type, values):
    if not db.execute('SELECT 1 FROM ref_page_types WHERE slug=?', (page_type,)).fetchone():
        raise ValueError('Тип страницы не найден')
    try:
        ids = set(map(int, values))
    except (ValueError, TypeError):
        raise ValueError('Некорректный список ответственных') from None
    allowed = {c['id'] for c in contacts(db, site)}
    if not ids <= allowed:
        raise ValueError('Можно выбрать только ответственных этого сайта')
    old = {r['contact_id']:r['coverage'] for r in db.execute('SELECT * FROM site_type_contacts WHERE site=? AND page_type=?', (site,page_type))}
    db.execute('DELETE FROM site_type_contacts WHERE site=? AND page_type=?', (site,page_type))
    for cid in ids:
        db.execute('INSERT INTO site_type_contacts VALUES (?,?,?,?)', (site,page_type,cid,old.get(cid,'')))
