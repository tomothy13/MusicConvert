import sqlite3
from pathlib import Path
import json
import subprocess
import os
import time


def init_db(db_path: str | Path):
    db_path = str(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute('PRAGMA foreign_keys = ON')
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS albums (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        directory TEXT,
        artist TEXT,
        art BLOB,
        zip_path TEXT,
        created_at REAL NOT NULL
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS songs (
        id INTEGER PRIMARY KEY,
        album_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        title TEXT,
        artist TEXT,
        duration REAL,
        track INTEGER,
        filesize INTEGER,
        created_at REAL NOT NULL,
        FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE CASCADE
    )
    ''')
    # Links table to track user-supplied links and status
    cur.execute('''
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY,
        link TEXT NOT NULL UNIQUE,
        status TEXT,
        created_at REAL NOT NULL
    )
    ''')
    conn.commit()
    # Ensure older DBs get the zip_path column if missing
    try:
        cur.execute("PRAGMA table_info(albums)")
        cols = [r[1] for r in cur.fetchall()]
        if 'zip_path' not in cols:
            cur.execute('ALTER TABLE albums ADD COLUMN zip_path TEXT')
            conn.commit()
    except Exception:
        pass
    return conn


def create_album(conn: sqlite3.Connection, name: str, directory: str | None = None):
    now = time.time()
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO albums(name,directory,created_at) VALUES(?,?,?)', (name, directory, now))
        conn.commit()
    except sqlite3.IntegrityError:
        # already exists
        pass
    cur.execute('SELECT id FROM albums WHERE name = ?', (name,))
    row = cur.fetchone()
    return row[0] if row else None


def update_album_art(conn: sqlite3.Connection, album_id: int, artist: str | None, art_bytes: bytes | None):
    cur = conn.cursor()
    try:
        if artist is not None and art_bytes is not None:
            cur.execute('UPDATE albums SET artist = ?, art = ? WHERE id = ?', (artist, art_bytes, album_id))
        elif artist is not None:
            cur.execute('UPDATE albums SET artist = ? WHERE id = ?', (artist, album_id))
        elif art_bytes is not None:
            cur.execute('UPDATE albums SET art = ? WHERE id = ?', (art_bytes, album_id))
        conn.commit()
    except Exception:
        conn.rollback()


def update_album_zip(conn: sqlite3.Connection, album_id: int, zip_path: str | None):
    cur = conn.cursor()
    try:
        cur.execute('UPDATE albums SET zip_path = ? WHERE id = ?', (zip_path, album_id))
        conn.commit()
    except Exception:
        conn.rollback()


def add_song(conn: sqlite3.Connection, album_id: int, filename: str, filepath: str, metadata: dict | None = None):
    now = time.time()
    metadata = metadata or {}
    title = metadata.get('title')
    artist = metadata.get('artist')
    duration = metadata.get('duration')
    track = metadata.get('track')
    try:
        filesize = os.path.getsize(filepath)
    except Exception:
        filesize = None
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO songs(album_id,filename,filepath,title,artist,duration,track,filesize,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (album_id, filename, filepath, title, artist, duration, track, filesize, now)
    )
    conn.commit()
    return cur.lastrowid


def add_link(conn: sqlite3.Connection, link: str, status: str | None = None):
    now = time.time()
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO links(link,status,created_at) VALUES(?,?,?)', (link, status, now))
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            cur.execute('UPDATE links SET status = ? WHERE link = ?', (status, link))
            conn.commit()
        except Exception:
            conn.rollback()


def list_links(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute('SELECT id,link,status,created_at FROM links ORDER BY created_at DESC')
    rows = cur.fetchall()
    return [dict(id=r[0], link=r[1], status=r[2], created_at=r[3]) for r in rows]


def list_albums(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute('SELECT id,name,directory,created_at FROM albums ORDER BY created_at DESC')
    rows = cur.fetchall()
    return [dict(id=r[0], name=r[1], directory=r[2], created_at=r[3]) for r in rows]


def list_songs(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute('''
    SELECT songs.id, songs.album_id, songs.filename, songs.filepath, songs.title, songs.artist, songs.duration, songs.track, songs.filesize, songs.created_at, albums.name
    FROM songs LEFT JOIN albums ON songs.album_id = albums.id
    ORDER BY songs.created_at DESC
    ''')
    rows = cur.fetchall()
    return [dict(id=r[0], album_id=r[1], filename=r[2], filepath=r[3], title=r[4], artist=r[5], duration=r[6], track=r[7], filesize=r[8], created_at=r[9], album_name=r[10]) for r in rows]


def get_album(conn: sqlite3.Connection, album_id: int):
    cur = conn.cursor()
    cur.execute('SELECT id,name,directory,created_at FROM albums WHERE id = ?', (album_id,))
    row = cur.fetchone()
    if not row:
        return None
    album = dict(id=row[0], name=row[1], directory=row[2], created_at=row[3])
    # SQLite doesn't support NULLS LAST syntax; order by whether track IS NULL first, then track, then filename
    cur.execute('SELECT id,filename,filepath,title,artist,duration,track,filesize,created_at FROM songs WHERE album_id = ? ORDER BY (track IS NULL), track, filename', (album_id,))
    songs = []
    for r in cur.fetchall():
        songs.append(dict(id=r[0], filename=r[1], filepath=r[2], title=r[3], artist=r[4], duration=r[5], track=r[6], filesize=r[7], created_at=r[8]))
    album['songs'] = songs
    return album


def get_song(conn: sqlite3.Connection, song_id: int):
    cur = conn.cursor()
    cur.execute('SELECT id,album_id,filename,filepath,title,artist,duration,track,filesize,created_at FROM songs WHERE id = ?', (song_id,))
    r = cur.fetchone()
    if not r:
        return None
    return dict(id=r[0], album_id=r[1], filename=r[2], filepath=r[3], title=r[4], artist=r[5], duration=r[6], track=r[7], filesize=r[8], created_at=r[9])


def extract_metadata(filepath: str):
    # Use ffprobe to extract basic metadata (title, artist, album, duration, track)
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', filepath
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
        fmt = data.get('format', {})
        tags = fmt.get('tags', {}) or {}
        duration = None
        try:
            duration = float(fmt.get('duration')) if fmt.get('duration') else None
        except Exception:
            duration = None
        track = tags.get('track') or tags.get('tracknumber')
        if track:
            try:
                track = int(str(track).split('/')[0])
            except Exception:
                track = None
        return {
            'title': tags.get('title') or None,
            'artist': tags.get('artist') or None,
            'album': tags.get('album') or None,
            'duration': duration,
            'track': track,
        }
    except Exception:
        # best-effort fallback: use filename as title
        return {'title': Path(filepath).stem}
