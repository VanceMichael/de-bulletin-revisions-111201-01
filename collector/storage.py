import hashlib
import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS raw_objects(
                digest TEXT PRIMARY KEY, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(
                id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                digest TEXT NOT NULL REFERENCES raw_objects(digest),
                UNIQUE(source,digest));
            CREATE TABLE IF NOT EXISTS records(
                record_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                import_id INTEGER NOT NULL REFERENCES imports(id));
            CREATE TABLE IF NOT EXISTS items(
                item_id INTEGER PRIMARY KEY,
                main_url TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                retracted_at TEXT,
                retract_reason TEXT,
                retract_source_date TEXT);
            CREATE TABLE IF NOT EXISTS links(
                link_id INTEGER PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                item_id INTEGER NOT NULL REFERENCES items(item_id),
                relation TEXT NOT NULL,
                etag TEXT,
                last_modified TEXT,
                body_digest TEXT,
                content_key TEXT,
                last_status INTEGER,
                last_fetch_at TEXT,
                last_failure TEXT);
            CREATE TABLE IF NOT EXISTS versions(
                version_id INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES items(item_id),
                seq INTEGER NOT NULL,
                body_digest TEXT NOT NULL,
                content_key TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                observed_via TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source_date TEXT,
                kind TEXT NOT NULL,
                UNIQUE(item_id,seq),
                UNIQUE(item_id,content_key));
            CREATE TABLE IF NOT EXISTS heads(
                item_id INTEGER PRIMARY KEY REFERENCES items(item_id),
                version_id INTEGER NOT NULL REFERENCES versions(version_id));
            CREATE TABLE IF NOT EXISTS fetch_events(
                event_id INTEGER PRIMARY KEY,
                link_url TEXT NOT NULL,
                item_id INTEGER,
                at TEXT NOT NULL,
                source_date TEXT,
                status INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT,
                digest TEXT);
        """)

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def ingest(self, source, raw, records):
        digest = hashlib.sha256(raw).hexdigest()
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO raw_objects VALUES (?,?)", (digest, raw))
            self.connection.execute("INSERT OR IGNORE INTO imports(source,digest) VALUES (?,?)", (source, digest))
            import_id = self.connection.execute("SELECT id FROM imports WHERE source=? AND digest=?", (source, digest)).fetchone()[0]
            for record in records:
                key = record.get('key')
                if not isinstance(key, str) or not key:
                    raise ValueError('记录缺少有效标识')
                encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
                self.connection.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(record_key) DO UPDATE SET payload=excluded.payload, import_id=excluded.import_id", (key, encoded, import_id))
        return digest

    def records(self):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT payload FROM records ORDER BY record_key")]

    def provenance(self, key):
        row = self.connection.execute("SELECT imports.source, imports.digest FROM records JOIN imports ON imports.id=records.import_id WHERE record_key=?", (key,)).fetchone()
        return dict(row) if row else None

    # ---- 持续追踪：原文保存（第一阶段，幂等） ----

    def save_raw(self, raw):
        """正文字节落库，内容寻址、可重复调用。独立事务提交。"""
        digest = hashlib.sha256(raw).hexdigest()
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO raw_objects VALUES (?,?)", (digest, raw))
        return digest

    def get_raw(self, digest):
        row = self.connection.execute("SELECT payload FROM raw_objects WHERE digest=?", (digest,)).fetchone()
        return bytes(row[0]) if row else None

    def get_link(self, url):
        return self.connection.execute("SELECT * FROM links WHERE url=?", (url,)).fetchone()

    def link_identity(self, url):
        row = self.connection.execute(
            "SELECT i.main_url, l.relation FROM links l JOIN items i ON i.item_id=l.item_id WHERE l.url=?",
            (url,)).fetchone()
        return (row['main_url'], row['relation']) if row else (url, 'original')

    def _ensure_item(self, main_url):
        self.connection.execute("INSERT OR IGNORE INTO items(main_url) VALUES (?)", (main_url,))
        return self.connection.execute("SELECT item_id FROM items WHERE main_url=?", (main_url,)).fetchone()[0]

    def _ensure_link(self, url, item_id, relation):
        self.connection.execute(
            """INSERT INTO links(url,item_id,relation) VALUES (?,?,?)
               ON CONFLICT(url) DO UPDATE SET item_id=excluded.item_id, relation=excluded.relation""",
            (url, item_id, relation))

    def _add_event(self, link_url, item_id, at, source_date, status, outcome, detail, digest):
        self.connection.execute(
            "INSERT INTO fetch_events(link_url,item_id,at,source_date,status,outcome,detail,digest) VALUES (?,?,?,?,?,?,?,?)",
            (link_url, item_id, at, source_date, status, outcome, detail, digest))

    # ---- 持续追踪：版本切换（第二阶段，单事务原子生效） ----

    def commit_observation(self, *, link_url, main_url, relation, digest, content_key,
                           title, content, etag, last_modified, fetched_at, source_date,
                           http_status, note=None):
        """登记一次 200/304 观察。版本插入、索引头切换、链接凭据在同一事务内完成。

        同一正文（按解析内容）对同一条目只产生一个版本，并发或重启重放只会
        造成一次有效切换。转载链接只更新自身凭据与缓存，不创建版本、不移动
        主线索引头。
        """
        if self.get_raw(digest) is None:
            raise ValueError('正文尚未持久化，不能登记版本')
        with self.transaction():
            item_id = self._ensure_item(main_url)
            self._ensure_link(link_url, item_id, relation)
            row = self.connection.execute(
                """SELECT i.status, h.version_id AS head_id FROM items i
                   LEFT JOIN heads h ON h.item_id=i.item_id WHERE i.item_id=?""",
                (item_id,)).fetchone()
            outcome, seq, switched = 'unchanged', None, False
            detail = note
            if row['status'] == 'retracted':
                outcome = 'ignored_retracted'
                detail = (note + '；' if note else '') + '条目已撤稿，正文仅留证不进入版本'
            elif relation == 'original' or row['head_id'] is None:
                current = self.connection.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM versions WHERE item_id=?", (item_id,)).fetchone()[0]
                cursor = self.connection.execute(
                    """INSERT INTO versions(item_id,seq,body_digest,content_key,title,content,observed_via,fetched_at,source_date,kind)
                       VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id,content_key) DO NOTHING""",
                    (item_id, current + 1, digest, content_key, title, content, link_url,
                     fetched_at, source_date, 'initial' if current == 0 else 'correction'))
                if cursor.rowcount == 1:
                    outcome = 'initial' if current == 0 else 'correction'
                    seq = current + 1
                    version_id = cursor.lastrowid
                    self.connection.execute(
                        """INSERT INTO heads(item_id,version_id) VALUES (?,?)
                           ON CONFLICT(item_id) DO UPDATE SET version_id=excluded.version_id""",
                        (item_id, version_id))
                    switched = row['head_id'] != version_id
            self.connection.execute(
                """UPDATE links SET etag=?, last_modified=?, body_digest=?, content_key=?,
                   last_status=?, last_fetch_at=?, last_failure=NULL WHERE url=?""",
                (etag, last_modified, digest, content_key, http_status, fetched_at, link_url))
            event_outcome = 'not_modified' if http_status == 304 else outcome
            self._add_event(link_url, item_id, fetched_at, source_date, http_status, event_outcome, detail, digest)
        return dict(outcome=outcome, seq=seq, switched=switched, item_id=item_id)

    def commit_failure(self, *, link_url, status, detail, fetched_at, source_date):
        """暂时抓取失败：只记录证据与链接状态，不触碰版本与索引头。"""
        with self.transaction():
            link = self.connection.execute("SELECT item_id FROM links WHERE url=?", (link_url,)).fetchone()
            item_id = link['item_id'] if link else None
            if link:
                self.connection.execute(
                    "UPDATE links SET last_status=?, last_fetch_at=?, last_failure=? WHERE url=?",
                    (status, fetched_at, detail, link_url))
            self._add_event(link_url, item_id, fetched_at, source_date, status, 'failed', detail, None)

    def commit_retraction(self, *, link_url, main_url, relation, reason, fetched_at, source_date, http_status):
        """明确撤稿：状态翻转幂等，并发或重放只产生一次有效变更。"""
        with self.transaction():
            item_id = self._ensure_item(main_url)
            self._ensure_link(link_url, item_id, relation)
            cursor = self.connection.execute(
                """UPDATE items SET status='retracted', retracted_at=?, retract_reason=?, retract_source_date=?
                   WHERE item_id=? AND status='active'""",
                (fetched_at, reason, source_date, item_id))
            changed = cursor.rowcount == 1
            self.connection.execute(
                "UPDATE links SET last_status=?, last_fetch_at=?, last_failure=NULL WHERE url=?",
                (http_status, fetched_at, link_url))
            if changed:
                self._add_event(link_url, item_id, fetched_at, source_date, http_status, 'retracted', reason, None)
        return changed

    # ---- 查询视图（只读，索引头即编辑所见） ----

    def items_view(self):
        result = []
        for item in self.connection.execute("SELECT * FROM items ORDER BY main_url"):
            item_id = item['item_id']
            head = self.connection.execute(
                """SELECT v.* FROM heads h JOIN versions v ON v.version_id=h.version_id
                   WHERE h.item_id=?""", (item_id,)).fetchone()
            count = self.connection.execute(
                "SELECT COUNT(*) FROM versions WHERE item_id=?", (item_id,)).fetchone()[0]
            links = []
            for link in self.connection.execute("SELECT * FROM links WHERE item_id=? ORDER BY url", (item_id,)):
                links.append(dict(
                    url=link['url'], relation=link['relation'], etag=link['etag'],
                    last_modified=link['last_modified'], last_status=link['last_status'],
                    last_fetch_at=link['last_fetch_at'], last_failure=link['last_failure'],
                    lagging=bool(head and link['content_key'] and link['content_key'] != head['content_key'])))
            entry = dict(
                main_url=item['main_url'], status=item['status'], versions=count,
                head=dict(seq=head['seq'], kind=head['kind'], title=head['title'],
                          content=head['content'], digest=head['body_digest'],
                          observed_via=head['observed_via'], fetched_at=head['fetched_at'],
                          source_date=head['source_date']) if head else None,
                links=links)
            if item['status'] == 'retracted':
                entry['retraction'] = dict(at=item['retracted_at'], reason=item['retract_reason'],
                                           source_date=item['retract_source_date'])
            result.append(entry)
        return result

    def versions_view(self, main_url):
        item = self.connection.execute("SELECT * FROM items WHERE main_url=?", (main_url,)).fetchone()
        if item is None:
            return None
        head = self.connection.execute("SELECT version_id FROM heads WHERE item_id=?", (item['item_id'],)).fetchone()
        head_id = head['version_id'] if head else None
        versions = [dict(seq=row['seq'], kind=row['kind'], title=row['title'], content=row['content'],
                         digest=row['body_digest'], observed_via=row['observed_via'],
                         fetched_at=row['fetched_at'], source_date=row['source_date'],
                         current=row['version_id'] == head_id)
                    for row in self.connection.execute(
                        "SELECT * FROM versions WHERE item_id=? ORDER BY seq", (item['item_id'],))]
        result = dict(main_url=main_url, status=item['status'], versions=versions)
        if item['status'] == 'retracted':
            result['retraction'] = dict(at=item['retracted_at'], reason=item['retract_reason'],
                                        source_date=item['retract_source_date'])
        return result

    def status_view(self):
        links = []
        for row in self.connection.execute(
                "SELECT l.*, i.main_url FROM links l JOIN items i ON i.item_id=l.item_id ORDER BY l.url"):
            head = self.connection.execute(
                """SELECT v.content_key FROM heads h JOIN versions v ON v.version_id=h.version_id
                   WHERE h.item_id=?""", (row['item_id'],)).fetchone()
            links.append(dict(
                url=row['url'], item=row['main_url'], relation=row['relation'],
                etag=row['etag'], last_modified=row['last_modified'],
                last_status=row['last_status'], last_fetch_at=row['last_fetch_at'],
                last_failure=row['last_failure'],
                lagging=bool(head and row['content_key'] and row['content_key'] != head[0])))
        failures = [dict(row) for row in self.connection.execute(
            "SELECT link_url, at, status, detail FROM fetch_events WHERE outcome='failed' ORDER BY event_id DESC LIMIT 20")]
        items = [dict(row) for row in self.connection.execute(
            "SELECT main_url, status FROM items ORDER BY main_url")]
        return dict(items=items, links=links, recent_failures=failures)

    def events_view(self, url=None):
        if url:
            rows = self.connection.execute(
                """SELECT e.* FROM fetch_events e LEFT JOIN items i ON i.item_id=e.item_id
                   WHERE e.link_url=? OR i.main_url=? ORDER BY e.event_id""", (url, url))
        else:
            rows = self.connection.execute("SELECT * FROM fetch_events ORDER BY event_id")
        return [dict(row) for row in rows]

    def close(self):
        self.connection.close()
