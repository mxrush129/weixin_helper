import hashlib
import json
import sqlite3
import time
from difflib import SequenceMatcher


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    captured_at REAL NOT NULL,
    wechat_time TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_group ON messages(group_name, captured_at, id);
CREATE TABLE IF NOT EXISTS snapshots (
    group_name TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    signatures TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_rows (
    group_name TEXT NOT NULL,
    source TEXT NOT NULL,
    row_key TEXT NOT NULL,
    PRIMARY KEY (group_name, source, row_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS images (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    source TEXT NOT NULL,
    row_key TEXT NOT NULL,
    filename TEXT,
    description TEXT,
    description_model TEXT
);
CREATE INDEX IF NOT EXISTS images_by_source ON images(source, row_key);
CREATE INDEX IF NOT EXISTS described_images ON images(filename, description_model) WHERE description IS NOT NULL;
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    hours INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS summaries_by_group ON summaries(group_name, id);
"""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def row_identity(row, signature):
    return digest((row.runtime_id, signature))


class Archive:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def get_setting(self, name, default):
        row = self.db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return json.loads(row["value"]) if row else default

    def save_setting(self, name, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, json.dumps(value)))

    def capture(self, group, snapshot):
        if not snapshot.rows:
            return 0
        signatures = [digest(row.signature) for row in snapshot.rows]
        serialized = json.dumps(signatures)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute("SELECT * FROM snapshots WHERE group_name = ?", (group,)).fetchone()
            overlap = set()
            if previous and (previous["source"] != snapshot.source or any(not row.runtime_id for row in snapshot.rows)):
                matcher = SequenceMatcher(None, json.loads(previous["signatures"]), signatures, autojunk=False)
                for block in matcher.get_matching_blocks():
                    overlap.update(range(block.b, block.b + block.size))

            count = 0
            wechat_time = ""
            for index, row in enumerate(snapshot.rows):
                if row.kind == "time":
                    wechat_time = row.text
                    continue
                if row.runtime_id:
                    row_key = row_identity(row, signatures[index])
                    inserted = self.db.execute(
                        "INSERT OR IGNORE INTO seen_rows VALUES (?, ?, ?)", (group, snapshot.source, row_key)
                    ).rowcount
                    if not inserted:
                        continue
                # Image placeholders look identical, so only a stable runtime ID can match one.
                if index in overlap and (row.kind != "image" or not row.runtime_id):
                    continue
                result = self.db.execute(
                    "INSERT INTO messages(group_name, captured_at, wechat_time, kind, body) VALUES (?, ?, ?, ?, ?)",
                    (group, now, wechat_time, row.kind, row.text),
                )
                if row.kind == "image":
                    key = row_identity(row, signatures[index]) if row.runtime_id else ""
                    self.db.execute(
                        "INSERT INTO images(message_id, source, row_key) VALUES (?, ?, ?)",
                        (result.lastrowid, snapshot.source, key),
                    )
                count += 1

            if not previous or (previous["source"], previous["signatures"]) != (snapshot.source, serialized):
                self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (group, snapshot.source, serialized))
        return count

    def recent(self, group, limit=250):
        rows = self.db.execute(
            """SELECT messages.*, images.filename, images.description
               FROM messages LEFT JOIN images ON images.message_id = messages.id
               WHERE group_name = ? ORDER BY captured_at DESC, id DESC LIMIT ?""", (group, limit)
        ).fetchall()
        return list(reversed(rows))

    def messages_since(self, group, hours):
        return self.db.execute(
            """SELECT messages.*, images.filename, images.description
               FROM messages LEFT JOIN images ON images.message_id = messages.id
               WHERE group_name = ? AND captured_at >= ? ORDER BY captured_at, id""",
            (group, time.time() - hours * 3600),
        ).fetchall()

    def pending_images(self, group, snapshot):
        rows = {
            row_identity(row, digest(row.signature)): row for row in snapshot.rows
            if row.kind == "image" and row.runtime_id
        }
        if not rows:
            return []
        placeholders = ",".join("?" for _ in rows)
        pending = self.db.execute(
            f"""SELECT images.message_id, images.row_key FROM images JOIN messages ON messages.id = images.message_id
                WHERE messages.group_name = ? AND images.source = ? AND images.filename IS NULL
                    AND images.row_key IN ({placeholders}) ORDER BY messages.id DESC""",
            (group, snapshot.source, *rows),
        ).fetchall()
        return [(record["message_id"], rows[record["row_key"]]) for record in pending]

    def image(self, message_id):
        return self.db.execute(
            "SELECT * FROM images WHERE message_id = ?", (message_id,)
        ).fetchone()

    def save_image(self, message_id, filename, previous=None):
        with self.db:
            result = self.db.execute(
                """UPDATE images SET filename = ?, description = NULL, description_model = NULL
                   WHERE message_id = ? AND filename IS ?""",
                (filename, message_id, previous),
            )
        return result.rowcount == 1

    def save_image_description(self, message_id, filename, description, model):
        with self.db:
            self.db.execute(
                "UPDATE images SET description = ?, description_model = ? WHERE message_id = ? AND filename = ?",
                (description, model, message_id, filename),
            )

    def image_description(self, filename, model):
        row = self.db.execute(
            "SELECT description FROM images WHERE filename = ? AND description_model = ? AND description IS NOT NULL LIMIT 1",
            (filename, model),
        ).fetchone()
        return row["description"] if row else None

    def latest_summary(self, group):
        return self.db.execute(
            "SELECT * FROM summaries WHERE group_name = ? ORDER BY id DESC LIMIT 1", (group,)
        ).fetchone()

    def save_summary(self, group, hours, message_count, body):
        with self.db:
            self.db.execute(
                "INSERT INTO summaries(group_name, created_at, hours, message_count, body) VALUES (?, ?, ?, ?, ?)",
                (group, time.time(), hours, message_count, body),
            )
