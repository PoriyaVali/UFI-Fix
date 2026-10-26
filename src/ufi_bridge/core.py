"""Bounded log access and separate, immutable support artifacts."""
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import locale
from pathlib import Path
import re
import sqlite3
import uuid

MAX_VIEW = 65536
MAX_SNAPSHOT = 8 * 1024 * 1024
MAX_REVISION = 128 * 1024
MODULES = {
    'emmc': ('eMMC ToolBox', 'eMMC_ToolBox.log', '/emmc'),
    'ufs': ('UFS ToolBox', 'UFS_ToolBox.log', '/ufs'),
    'android': ('Android ToolBox', 'Android_ToolBox.log', '/android'),
    'chip': ('CHIP Prog', 'ChipProg.log', '/chip'),
}


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(data):
    return hashlib.sha256(data).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def redact(text):
    """Best-effort support redaction, not a guarantee of anonymization."""
    text = re.sub(r'(?im)(\b(?:serial(?:\s+number)?|imei\d*|meid|imsi|iccid|cid|'
                  r'password|auth(?:entication)?\s+key|access\s+token|token)\b\s*[:=]\s*)'
                  r'[^\r\n]+', r'\1[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    return re.sub(r'(?i)([A-Z]:\\Users\\)[^\\\r\n]+', r'\1[USER]', text)


def text_view(data, limit=MAX_VIEW, at_tail=False):
    """Decode a bounded tail; report truncation and any lossy fallback."""
    enc = 'utf-8-sig'
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        enc = 'utf-16-le' if data.startswith(b'\xff\xfe') else 'utf-16-be'
    start = max(0, len(data) - limit)
    if enc.startswith('utf-16') and start % 2:
        start += 1
    chunk = data[start:]
    lossy = False
    try:
        text = chunk.decode(enc)
    except UnicodeDecodeError:
        # UTF-8 byte windows can start mid-character. A tail is a view, not
        # a path, so replacement is permitted and explicitly reported here.
        if start or at_tail:
            text = chunk.decode(enc, errors='replace')
            lossy = True
        else:
            enc = locale.getencoding()
            text = chunk.decode(enc, errors='replace')
            lossy = True
    return {'text': redact(text.lstrip('\ufeff')), 'encoding': enc,
            'lossy': lossy, 'truncated': bool(start), 'offset': start,
            'redaction': 'best_effort', 'untrusted_content': True}


def window_view(chunk, header, start, size):
    encoding = ('utf-16-le' if header.startswith(b'\xff\xfe') else
                'utf-16-be' if header.startswith(b'\xfe\xff') else 'utf-8')
    if start + len(chunk) < size:
        try:
            chunk.decode(encoding)
        except UnicodeDecodeError as exc:
            if exc.reason in ('unexpected end of data', 'truncated data') and exc.end == len(chunk):
                # Leave a split code point for the next page rather than lose it.
                chunk = chunk[:exc.start]
    payload = header[:2] + chunk if start and encoding.startswith('utf-16') else chunk
    view = text_view(payload, max(1, len(payload)), at_tail=bool(start))
    view.update(file_bytes=size, offset=start, next_offset=start + len(chunk),
                truncated=start > 0 or start + len(chunk) < size,
                has_more=start + len(chunk) < size, window_sha256=digest(chunk))
    return view


def saved_view(data, offset=None, limit=MAX_VIEW):
    start = max(0, len(data) - limit) if offset is None else offset
    if start > len(data):
        raise BridgeError('invalid_offset', 'Offset exceeds saved snapshot size.')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')) and start % 2:
        if offset is not None:
            raise BridgeError('invalid_offset', 'UTF-16 byte offsets must be even.')
        start += 1
    return window_view(data[start:start + limit], data[:4], start, len(data))


@dataclass(frozen=True)
class Source:
    source_id: str
    path: Path
    root: Path

    def read(self, limit):
        # Tool arguments select fixed source IDs, never arbitrary file paths.
        path = self.path.resolve()
        if path.parent != self.root.resolve():
            raise BridgeError('source_outside_root', 'Log redirects outside its configured directory.')
        try:
            with path.open('rb') as stream:
                data = stream.read(limit + 1)
        except FileNotFoundError:
            raise BridgeError('source_missing', 'The selected log does not exist.') from None
        except OSError:
            raise BridgeError('source_unreadable', 'The selected log could not be read.') from None
        if len(data) > limit:
            raise BridgeError('source_too_large', 'Log exceeds the snapshot size limit; use the bounded log view.')
        return data

    def tail(self, limit, offset=None):
        path = self.path.resolve()
        if path.parent != self.root.resolve():
            raise BridgeError('source_outside_root', 'Log redirects outside its configured directory.')
        try:
            with path.open('rb') as stream:
                header = stream.read(4)
                stream.seek(0, 2)
                size = stream.tell()
                start = max(0, size - limit) if offset is None else offset
                if start > size:
                    raise BridgeError('invalid_offset', 'Offset exceeds current log size; log may have rotated.')
                if header.startswith((b'\xff\xfe', b'\xfe\xff')) and start % 2:
                    if offset is not None:
                        raise BridgeError('invalid_offset', 'UTF-16 byte offsets must be even.')
                    start += 1
                stream.seek(start)
                data = stream.read(limit)
        except FileNotFoundError:
            raise BridgeError('source_missing', 'The selected log does not exist.') from None
        except OSError:
            raise BridgeError('source_unreadable', 'The selected log could not be read.') from None
        view = window_view(data, header, start, size)
        view['source'] = self.source_id
        return view


class ArtifactStore:
    """API inserts only. Each revision points to an unchanged original snapshot."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = self.directory / 'support.sqlite3'

    @contextmanager
    def connect(self, create=False):
        if not create and not self.path.is_file():
            raise BridgeError('artifact_missing', 'No saved support artifacts exist.')
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            if create:
                connection.executescript('''
CREATE TABLE IF NOT EXISTS objects (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
 base_id TEXT, created TEXT NOT NULL, sha256 TEXT NOT NULL,
 payload BLOB NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 seq INTEGER PRIMARY KEY, created TEXT NOT NULL, operation TEXT NOT NULL,
 object_id TEXT NOT NULL, sha256 TEXT NOT NULL);
''')
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def object_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            raise BridgeError('invalid_id', 'Artifact ID must be 32 lowercase hexadecimal characters.')
        return value

    @staticmethod
    def metadata(row):
        return {key: row[key] for key in ('id', 'kind', 'source', 'base_id', 'created', 'sha256', 'note')}

    def load(self, object_id, kind):
        object_id = self.object_id(object_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM objects WHERE id=? AND kind=?', (object_id, kind)).fetchone()
        if row is None:
            raise BridgeError('artifact_missing', 'The requested artifact does not exist.')
        if digest(row['payload']) != row['sha256']:
            raise BridgeError('artifact_corrupt', 'Saved artifact checksum does not match.')
        return row

    def insert(self, kind, source, payload, note='', base_id=None, expected_sha256=None):
        object_id, created, checksum = uuid.uuid4().hex, utcnow(), digest(payload)
        with self.connect(create=True) as db:
            if kind == 'revision':
                self.object_id(base_id)
                original = db.execute('SELECT * FROM objects WHERE id=? AND kind=?',
                                      (base_id, 'snapshot')).fetchone()
                if original is None:
                    raise BridgeError('artifact_missing', 'Original snapshot does not exist.')
                if digest(original['payload']) != original['sha256']:
                    raise BridgeError('artifact_corrupt', 'Original snapshot checksum does not match.')
                if original['sha256'] != expected_sha256:
                    raise BridgeError('snapshot_conflict', 'Expected original checksum does not match.')
                source = original['source']
            db.execute('INSERT INTO objects VALUES (?,?,?,?,?,?,?,?)',
                       (object_id, kind, source, base_id, created, checksum, payload, note))
            db.execute('INSERT INTO audit(created,operation,object_id,sha256) VALUES (?,?,?,?)',
                       (created, kind + '.create', object_id, checksum))
        return {'id': object_id, 'kind': kind, 'source': source, 'base_id': base_id,
                'created': created, 'sha256': checksum, 'note': note, 'bytes': len(payload)}

    def revisions(self, snapshot_id):
        self.load(snapshot_id, 'snapshot')
        with self.connect() as db:
            rows = db.execute('SELECT * FROM objects WHERE base_id=? AND kind=? '
                              'ORDER BY created DESC LIMIT 100', (snapshot_id, 'revision')).fetchall()
        return [self.metadata(row) for row in rows]

    def audit(self, limit):
        if not self.path.exists():
            return []
        with self.connect() as db:
            rows = db.execute('SELECT * FROM audit ORDER BY seq DESC LIMIT ?', (limit,)).fetchall()
        return [dict(row) for row in rows]
