"""Хранение: medkarta.db (SQLite). Схема — из документа «Медкарта — модель данных»."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS persons (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, sex TEXT, birth_date TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES persons(id),
  kind TEXT NOT NULL DEFAULT 'lab', collected_at TEXT, reported_at TEXT, facility TEXT, order_no TEXT,
  doctor TEXT, title TEXT, summary TEXT, notes TEXT, source TEXT, related_ids TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  name TEXT, mime TEXT, size INTEGER, sha256 TEXT, path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS studies (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  panel_key TEXT, title_raw TEXT, material TEXT, section_raw TEXT, method_raw TEXT, comment_raw TEXT,
  modality TEXT, region TEXT, sort INTEGER);
CREATE TABLE IF NOT EXISTS results (
  id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
  analyte_key TEXT, key_manual INTEGER DEFAULT 0, match_via TEXT,
  name_raw TEXT, abbr_raw TEXT, section_raw TEXT, result_type TEXT NOT NULL,
  value_raw TEXT, value REAL, value_op TEXT, value_std REAL, value_text TEXT, value_rank INTEGER, value_bool INTEGER,
  unit_raw TEXT, unit TEXT, unit_std TEXT, ref_raw TEXT, ref_selected TEXT, ref_low REAL, ref_high REAL, ref_source TEXT,
  flag_raw TEXT, flag_lab TEXT, flag TEXT, comment_raw TEXT, codes_json TEXT, organisms_json TEXT,
  method_changed INTEGER DEFAULT 0, needs_review INTEGER DEFAULT 0, review_reason TEXT, sort INTEGER);
CREATE INDEX IF NOT EXISTS results_key ON results(analyte_key);
CREATE TABLE IF NOT EXISTS lesions (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES persons(id), organ TEXT, label TEXT,
  first_seen TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  study_id TEXT, lesion_id TEXT REFERENCES lesions(id), organ TEXT, location_raw TEXT, kind TEXT,
  size_a REAL, size_b REAL, size_c REAL, volume REAL, features_raw TEXT,
  score_system TEXT, score_value TEXT, comment_raw TEXT, sort INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS user_aliases (
  alias_norm TEXT, material TEXT, unit TEXT, analyte_key TEXT, PRIMARY KEY (alias_norm, material, unit));
"""

DOC_FIELDS = ["person_id", "kind", "collected_at", "reported_at", "facility", "order_no", "doctor", "title",
              "summary", "notes", "source", "related_ids", "ocr_text", "form_template", "form_values"]
STUDY_FIELDS = ["panel_key", "title_raw", "material", "section_raw", "method_raw", "comment_raw", "modality", "region",
                "form_template"]
# Колонки, добавленные после 0.1: (таблица, колонка, тип) — добавляются в старые базы при открытии
MIGRATIONS = [("documents", "ocr_text", "TEXT"), ("documents", "form_template", "TEXT"), ("documents", "form_values", "TEXT"),
              ("studies", "form_template", "TEXT")]
RESULT_FIELDS = ["analyte_key", "key_manual", "match_via", "name_raw", "abbr_raw", "section_raw", "result_type",
                 "value_raw", "value", "value_op", "value_std", "value_text", "value_rank", "value_bool",
                 "unit_raw", "unit", "unit_std", "ref_raw", "ref_selected", "ref_low", "ref_high", "ref_source",
                 "flag_raw", "flag_lab", "flag", "comment_raw", "codes_json", "organisms_json",
                 "method_changed", "needs_review", "review_reason"]
FINDING_FIELDS = ["study_id", "lesion_id", "organ", "location_raw", "kind", "size_a", "size_b", "size_c", "volume",
                  "features_raw", "score_system", "score_value", "comment_raw"]


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now():
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.con.executescript(SCHEMA)
            for table, col, typ in MIGRATIONS:
                cols = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self.con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            # версия 0.1 хранила источник нормы текстом по-русски; теперь — коды
            self.con.execute("UPDATE results SET ref_source='blank' WHERE ref_source='бланк'")
            self.con.execute("UPDATE results SET ref_source='ref|' || substr(ref_source, 13) "
                             "WHERE ref_source LIKE 'справочник: %'")
            for old_v, new_v in (("название", "name"), ("аббревиатура", "abbr"), ("личный синоним", "user_alias"),
                                 ("вручную", "manual")):
                self.con.execute("UPDATE results SET match_via=? WHERE match_via=?", (new_v, old_v))
            self.con.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    # ---------- люди ----------
    def persons(self):
        return self.q("SELECT * FROM persons ORDER BY created_at")

    def save_person(self, p):
        with self.lock:
            if p.get("id"):
                self.con.execute("UPDATE persons SET name=?, sex=?, birth_date=? WHERE id=?",
                                 (p["name"], p.get("sex"), p.get("birth_date"), p["id"]))
            else:
                p["id"] = new_id("p")
                self.con.execute("INSERT INTO persons VALUES (?,?,?,?,?)",
                                 (p["id"], p["name"], p.get("sex"), p.get("birth_date"), now()))
            self.con.commit()
        return p["id"]

    def delete_person(self, pid):
        if self.one("SELECT 1 x FROM documents WHERE person_id=?", (pid,)):
            raise ValueError("person_has_docs")
        with self.lock:
            self.con.execute("DELETE FROM persons WHERE id=?", (pid,))
            self.con.commit()

    # ---------- документы ----------
    def list_documents(self, person_id):
        return self.q("""
          SELECT d.id, d.kind, d.collected_at, d.facility, d.title, d.order_no, d.doctor,
            substr(coalesce(d.summary,'') || ' ' || coalesce(d.notes,'') || ' ' || coalesce(d.ocr_text,''), 1, 30000) search_text,
            (SELECT COUNT(*) FROM files f WHERE f.document_id=d.id) n_files,
            (SELECT COUNT(*) FROM results r JOIN studies s ON r.study_id=s.id WHERE s.document_id=d.id) n_results,
            (SELECT COUNT(*) FROM results r JOIN studies s ON r.study_id=s.id WHERE s.document_id=d.id
               AND r.flag IN ('low','high')) n_out,
            (SELECT COUNT(*) FROM results r JOIN studies s ON r.study_id=s.id WHERE s.document_id=d.id
               AND r.needs_review=1) n_review,
            (SELECT COUNT(*) FROM findings fi WHERE fi.document_id=d.id) n_findings
          FROM documents d WHERE d.person_id=? ORDER BY d.collected_at DESC, d.created_at DESC""", (person_id,))

    def get_document(self, doc_id):
        d = self.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not d:
            return None
        try:
            d["form_values"] = json.loads(d["form_values"]) if d.get("form_values") else {}
        except ValueError:
            d["form_values"] = {}
        d["files"] = self.q("SELECT * FROM files WHERE document_id=? ORDER BY name", (doc_id,))
        d["studies"] = self.q("SELECT * FROM studies WHERE document_id=? ORDER BY sort", (doc_id,))
        for s in d["studies"]:
            s["results"] = self.q("SELECT * FROM results WHERE study_id=? ORDER BY sort", (s["id"],))
            for r in s["results"]:
                r["codes"] = json.loads(r["codes_json"]) if r.get("codes_json") else []
        d["findings"] = self.q("SELECT * FROM findings WHERE document_id=? ORDER BY sort", (doc_id,))
        return d

    def find_by_order(self, person_id, facility, order_no, exclude_id=None):
        if not order_no:
            return None
        rows = self.q("SELECT id, facility FROM documents WHERE person_id=? AND order_no=? AND id IS NOT ?",
                      (person_id, order_no, exclude_id))
        return rows[0]["id"] if rows else None

    def find_file_by_hash(self, sha):
        return self.one("SELECT f.*, d.title, d.collected_at FROM files f JOIN documents d ON d.id=f.document_id "
                        "WHERE f.sha256=?", (sha,))

    def save_document(self, d):
        """Сохраняет документ целиком (исследования, результаты, находки пересоздаются)."""
        d = dict(d)
        if isinstance(d.get("form_values"), (dict, list)):
            d["form_values"] = json.dumps(d["form_values"], ensure_ascii=False) if d["form_values"] else None
        with self.lock:
            cur = self.con.cursor()
            ts = now()
            if d.get("id") and self.one("SELECT 1 x FROM documents WHERE id=?", (d["id"],)):
                cur.execute(f"UPDATE documents SET {', '.join(f + '=?' for f in DOC_FIELDS)}, updated_at=? WHERE id=?",
                            [d.get(f) for f in DOC_FIELDS] + [ts, d["id"]])
                cur.execute("DELETE FROM studies WHERE document_id=?", (d["id"],))
                cur.execute("DELETE FROM findings WHERE document_id=?", (d["id"],))
            else:
                d["id"] = new_id("d")
                cur.execute(f"INSERT INTO documents (id, {', '.join(DOC_FIELDS)}, created_at, updated_at) "
                            f"VALUES ({', '.join('?' * (len(DOC_FIELDS) + 3))})",
                            [d["id"]] + [d.get(f) for f in DOC_FIELDS] + [ts, ts])
            keep = {f["id"] for f in d.get("files", []) if f.get("id")}
            for f in self.q("SELECT id FROM files WHERE document_id=?", (d["id"],)):
                if f["id"] not in keep:
                    cur.execute("DELETE FROM files WHERE id=?", (f["id"],))
            for f in d.get("files", []):
                if not f.get("id"):
                    f["id"] = new_id("f")
                    cur.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)",
                                (f["id"], d["id"], f.get("name"), f.get("mime"), f.get("size"), f.get("sha256"), f["path"]))
            for si, s in enumerate(d.get("studies", [])):
                sid = new_id("s")
                cur.execute(f"INSERT INTO studies (id, document_id, {', '.join(STUDY_FIELDS)}, sort) "
                            f"VALUES ({', '.join('?' * (len(STUDY_FIELDS) + 3))})",
                            [sid, d["id"]] + [s.get(f) for f in STUDY_FIELDS] + [si])
                for ri, r in enumerate(s.get("results", [])):
                    r = dict(r)
                    r["codes_json"] = json.dumps(r.get("codes") or [], ensure_ascii=False) if r.get("codes") else None
                    r["key_manual"] = 1 if r.get("key_manual") else 0
                    r["method_changed"] = 1 if r.get("method_changed") else 0
                    cur.execute(f"INSERT INTO results (id, study_id, {', '.join(RESULT_FIELDS)}, sort) "
                                f"VALUES ({', '.join('?' * (len(RESULT_FIELDS) + 3))})",
                                [new_id("r"), sid] + [r.get(f) for f in RESULT_FIELDS] + [ri])
            for fi, f in enumerate(d.get("findings", [])):
                cur.execute(f"INSERT INTO findings (id, document_id, {', '.join(FINDING_FIELDS)}, sort) "
                            f"VALUES ({', '.join('?' * (len(FINDING_FIELDS) + 3))})",
                            [new_id("fi"), d["id"]] + [f.get(k) for k in FINDING_FIELDS] + [fi])
            self.con.commit()
        return d["id"]

    def delete_document(self, doc_id):
        files = self.q("SELECT path FROM files WHERE document_id=?", (doc_id,))
        with self.lock:
            self.con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self.con.commit()
        return [f["path"] for f in files]

    def add_files(self, doc_id, files):
        with self.lock:
            for f in files:
                self.con.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)",
                                 (new_id("f"), doc_id, f.get("name"), f.get("mime"), f.get("size"), f.get("sha256"), f["path"]))
            self.con.commit()

    def path_in_use(self, rel):
        return bool(self.one("SELECT 1 x FROM files WHERE path=?", (rel,)))

    # ---------- служебные значения ----------
    def get_meta(self, key):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def set_meta(self, key, value):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
            self.con.commit()

    def all_document_ids(self):
        return [r["id"] for r in self.q("SELECT id FROM documents")]

    # ---------- синонимы пользователя ----------
    def user_aliases(self):
        return {(r["alias_norm"], r["material"], r["unit"]): r["analyte_key"] for r in self.q("SELECT * FROM user_aliases")}

    def save_user_alias(self, alias_norm, material, unit, key):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO user_aliases VALUES (?,?,?,?)", (alias_norm, material, unit, key))
            self.con.commit()

    # ---------- показатели во времени ----------
    def series_rows(self, person_id):
        return self.q("""
          SELECT r.*, d.collected_at, d.id doc_id, d.facility FROM results r
          JOIN studies s ON r.study_id=s.id JOIN documents d ON s.document_id=d.id
          WHERE d.person_id=? AND r.result_type IN ('numeric','titer') AND r.value IS NOT NULL
          ORDER BY d.collected_at""", (person_id,))
