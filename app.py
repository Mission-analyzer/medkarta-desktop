"""Медкарта — личный архив анализов и исследований. Запуск: python app.py"""
import base64
import functools
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import traceback
from datetime import date
from pathlib import Path

import webview

from db import DB, new_id
from i18n import LANGS, default_lang, msg
from parsers import parse_pdf
import forms
import ocr
from observations import Observations
import export_phone
from recognize import DEFAULT_MODEL, RecognizeError, recognize
from reference import Reference, family, norm, unit_label

APP_NAME = "Медкарта"
APP_VERSION = "0.7.1"
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # где лежат ui и reference
# Папка самой программы: для .exe — рядом с Medkarta.exe, для исходников — рядом с app.py
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# Переносной режим: если рядом с программой лежит файл portable.txt, всё хранится в папке программы
# (настройки — settings.json, архив — подпапка data). На компьютере ничего не записывается.
PORTABLE = (APP_DIR / "portable.txt").exists()
if PORTABLE:
    CONFIG_DIR = APP_DIR
    DEFAULT_DATA = APP_DIR / "data"
else:
    CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "Medkarta"
    DEFAULT_DATA = Path.home() / "Documents" / "Medkarta"
CONFIG = CONFIG_DIR / "settings.json"
ALLOWED = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
           ".webp": "image/webp"}


def dialog(kind):
    """Тип диалога для pywebview 4–6: FileDialog.OPEN или устаревшее OPEN_DIALOG."""
    fd = getattr(webview, "FileDialog", None)
    if fd is not None and hasattr(fd, kind):
        return getattr(fd, kind)
    return getattr(webview, f"{kind}_DIALOG")


def load_settings():
    s = {"data_dir": str(DEFAULT_DATA), "api_key": "", "model": DEFAULT_MODEL, "person_id": None, "lang": None}
    try:
        s.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if PORTABLE:  # буква флешки меняется, поэтому путь к архиву всегда относительный
        s["data_dir"] = str(DEFAULT_DATA)
    if s.get("lang") not in LANGS:
        s["lang"] = default_lang()
    return s


def save_settings(s):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    out = dict(s)
    if PORTABLE:
        out.pop("data_dir", None)
    CONFIG.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


class UserError(Exception):
    """Ошибка для пользователя: ключ сообщения из i18n.py и подстановки."""

    def __init__(self, key, **kw):
        super().__init__(key)
        self.key, self.kw = key, kw


def api_method(fn):
    """Ошибки возвращаются в интерфейс как {"error": "..."} на языке пользователя, а не роняют окно."""
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        lang = self._settings.get("lang", "en")
        try:
            return fn(self, *a, **kw)
        except UserError as e:
            return {"error": msg(e.key, lang, **e.kw)}
        except RecognizeError as e:
            return {"error": msg(e.code, lang, detail=e.detail)}
        except ValueError as e:
            return {"error": msg(str(e), lang)}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return {"error": msg("internal", lang, detail=e)}
    return wrap


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name):
    return re.sub(r"[^\w.\-]+", "_", name, flags=re.U)[:80]


class Api:
    def __init__(self):
        self._window = None
        self._base = BASE
        self._settings = load_settings()
        self._ref = Reference(BASE / "reference" / "reference.json")
        self._open_data()

    def _open_data(self):
        self._data = Path(self._settings["data_dir"])
        (self._data / "files").mkdir(parents=True, exist_ok=True)
        (self._data / "templates").mkdir(parents=True, exist_ok=True)
        self._forms = forms.FormLibrary(BASE / "templates", self._data / "templates")
        self._db = DB(self._data / "medkarta.db")
        self._obs = Observations(self._db)
        # Справочник обновился — пересопоставляем уже внесённые документы, чтобы новые показатели появились и в них
        fp = self._ref.data.get("fingerprint")
        if fp and self._db.get_meta("reference_fingerprint") != fp:
            self._rematch_all()
            self._db.set_meta("reference_fingerprint", fp)

    def _rematch_all(self):
        n = 0
        for doc_id in self._db.all_document_ids():
            d = self._db.get_document(doc_id)
            self._rematch(d)
            self._db.save_document(d)
            n += 1
        return n

    def _abs(self, rel):
        return self._data / rel

    # ---------- общее ----------
    @api_method
    def get_state(self):
        persons = self._db.persons()
        pid = self._settings.get("person_id")
        if pid not in {p["id"] for p in persons}:
            pid = persons[0]["id"] if persons else None
        return {
            "persons": persons, "person_id": pid, "data_dir": str(self._data), "portable": PORTABLE,
            "lang": self._settings["lang"], "version": APP_VERSION,
            "has_api_key": bool(self._settings.get("api_key")), "model": self._settings.get("model") or DEFAULT_MODEL,
            "analytes": [{"key": a["key"], "name_ru": a["name_ru"], "name_uk": a["name_uk"],
                          "name_en": a.get("name_en") or a["name_ru"], "unit_std": a["unit_std"],
                          "precision": a["precision"], "chart_scale": a["chart_scale"]}
                         for a in self._ref.data["analytes"]],
            "panels": [{"key": p["key"], "name_ru": p["name_ru"], "name_uk": p["name_uk"],
                        "name_en": p.get("name_en") or p["name_ru"]} for p in self._ref.data["panels"]],
            "reference_source": self._ref.data.get("source"),
            "forms": self._forms.summary(self._settings["lang"]), "form_errors": self._forms.errors,
            "ocr": ocr.status(), "templates_dir": str(self._data / "templates"),
        }

    @api_method
    def set_person(self, pid):
        self._settings["person_id"] = pid
        save_settings(self._settings)
        return True

    @api_method
    def save_settings(self, s):
        if "api_key" in s:
            self._settings["api_key"] = (s["api_key"] or "").strip()
        if s.get("model"):
            self._settings["model"] = s["model"].strip()
        if s.get("lang") in LANGS:
            self._settings["lang"] = s["lang"]
        save_settings(self._settings)
        return True

    @api_method
    def choose_data_dir(self):
        if PORTABLE:
            raise UserError("portable_dir")
        res = self._window.create_file_dialog(dialog("FOLDER"))
        if not res:
            return None
        new = Path(res[0] if isinstance(res, (list, tuple)) else res)
        # Можно выбрать папку с архивом или пустую папку (там будет создан новый архив, например тестовый).
        if new != self._data and not (new / "medkarta.db").exists() and any(new.iterdir()):
            raise UserError("dir_no_archive")
        self._settings["data_dir"] = str(new)
        save_settings(self._settings)
        self._open_data()
        return str(new)

    @api_method
    def open_data_dir(self):
        os.startfile(str(self._data))  # noqa: S606 (Windows)
        return True

    # ---------- люди ----------
    @api_method
    def save_person(self, p):
        if not (p.get("name") or "").strip():
            raise UserError("name_required")
        p["name"] = p["name"].strip()
        pid = self._db.save_person(p)
        if not self._settings.get("person_id"):
            self.set_person(pid)
        return pid

    @api_method
    def delete_person(self, pid):
        self._db.delete_person(pid)
        return True

    # ---------- документы ----------
    @api_method
    def list_documents(self, person_id):
        return self._db.list_documents(person_id)

    @api_method
    def get_document(self, doc_id):
        d = self._db.get_document(doc_id)
        if not d:
            raise UserError("doc_not_found")
        return d

    @api_method
    def delete_document(self, doc_id):
        for rel in self._db.delete_document(doc_id):
            if not self._db.path_in_use(rel):
                try:
                    self._abs(rel).unlink()
                except OSError:
                    pass
        return True

    @api_method
    def file_data_url(self, rel, mime):
        if not mime or not mime.startswith("image/"):
            return None
        p = self._abs(rel)
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()

    @api_method
    def open_file(self, rel):
        os.startfile(str(self._abs(rel)))  # noqa: S606
        return True

    # ---------- импорт ----------
    @api_method
    def pick_files(self):
        res = self._window.create_file_dialog(
            dialog("OPEN"), allow_multiple=True, directory=str(APP_DIR) if PORTABLE else "",
            file_types=({"ru": "Медицинские документы", "uk": "Медичні документи", "en": "Medical documents"}
                        [self._settings["lang"]] + " (*.pdf;*.jpg;*.jpeg;*.png;*.webp)",
                        {"ru": "Все файлы", "uk": "Усі файли", "en": "All files"}[self._settings["lang"]] + " (*.*)"))
        return list(res or [])

    def _store_file(self, src):
        src = Path(src)
        ext = src.suffix.lower()
        if ext not in ALLOWED:
            raise UserError("file_type", name=src.name)
        h = sha256(src)
        dup = self._db.find_file_by_hash(h)
        if dup:
            return None, dup
        rel = Path("files") / str(date.today().year) / f"{h[:12]}_{safe_name(src.stem)}{ext}"
        (self._data / rel.parent).mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, self._data / rel)
        return {"name": src.name, "mime": ALLOWED[ext], "size": src.stat().st_size, "sha256": h,
                "path": rel.as_posix()}, None

    @api_method
    def import_file(self, path, person_id, do_recognize=True):
        f, dup = self._store_file(path)
        if dup:
            return {"status": "duplicate_file", "doc_id": dup["document_id"], "name": Path(path).name,
                    "title": dup.get("title"), "collected_at": dup.get("collected_at")}
        draft = self._empty_draft(person_id)
        draft["files"] = [f]
        draft["title"] = Path(path).stem
        # 1) локальный шаблон лаборатории (текстовый PDF) — без интернета и без ключа
        if f["mime"] == "application/pdf":
            lab, rec = parse_pdf(self._abs(f["path"]), self._person(person_id))
            if rec and rec.get("kind") == "report":
                doc_id = self._db.find_by_order(person_id, None, rec.get("order_no"))
                if doc_id:                       # отчёт-приложение к уже внесённому бланку
                    self._db.add_files(doc_id, [f])
                    return {"status": "attached", "doc_id": doc_id, "name": Path(path).name}
                draft.update(title=rec.get("title") or draft["title"], order_no=rec.get("order_no") or "",
                             facility=rec.get("facility") or "", collected_at=rec.get("collected_at") or draft["collected_at"])
                draft["source"] = "local:" + lab
                return {"status": "draft", "draft": self._rematch(draft)}
            if rec:
                self._apply_recognized(draft, rec)
                draft["source"] = "local:" + lab
                return {"status": "draft", "draft": self._rematch(draft)}
        # 2) скан или фото: локальный OCR -> поиск по тексту и узнавание бланка по шаблону
        draft["_file_title"] = draft["title"]
        if ocr.status()["available"]:
            text = ocr.ocr_file(self._abs(f["path"]), f["mime"])
            if text:
                draft["ocr_text"] = text
                tid, score = self._forms.match(text)
                if tid:
                    t = self._forms.get(tid)
                    draft.update(form_template=tid, kind=t.get("kind", "imaging"), source="form:" + tid,
                                 title=forms._tr(t["title"], "uk"), facility=t.get("facility") or "")
                    return {"status": "draft", "draft": self._rematch(draft)}
                draft["source"] = "ocr"
                if ocr.is_mostly_unreadable(text):
                    draft["ocr_unreadable"] = True
        # 3) распознавание через Claude, если есть ключ
        if do_recognize and self._settings.get("api_key"):
            try:
                rec = recognize(self._abs(f["path"]), f["mime"], self._settings["api_key"], self._settings.get("model"))
                self._apply_recognized(draft, rec)
                draft["source"] = "ai"
            except RecognizeError as e:
                draft["recognize_error"] = msg(e.code, self._settings["lang"], detail=e.detail)
        return {"status": "draft", "draft": self._rematch(draft)}

    def _empty_draft(self, person_id):
        return {"id": None, "person_id": person_id, "kind": "lab", "collected_at": date.today().isoformat(),
                "reported_at": None, "facility": "", "order_no": "", "doctor": "", "title": "", "summary": "",
                "notes": "", "source": "manual", "related_ids": None, "files": [], "studies": [], "findings": [],
                "new_files": []}

    def _apply_recognized(self, d, rec):
        for k in ("kind", "facility", "order_no", "collected_at", "reported_at", "doctor", "title", "summary"):
            if rec.get(k):
                d[k] = rec[k]
        pat = rec.get("patient") or {}
        d["recognized_patient"] = pat
        d["studies"] = []
        for s in rec.get("studies") or []:
            d["studies"].append({
                "title_raw": s.get("title_raw"), "material": s.get("material") or "other",
                "method_raw": s.get("method_raw"), "section_raw": None, "comment_raw": None,
                "results": [{k: r.get(k) for k in ("name_raw", "value_raw", "unit_raw", "ref_raw", "ref_selected",
                                                    "flag_raw", "comment_raw", "method_changed", "codes")}
                            for r in s.get("results") or []]})
        d["findings"] = []
        for f in rec.get("findings") or []:
            sizes = [f.get("size_a"), f.get("size_b"), f.get("size_c")]
            sizes = [float(x) if isinstance(x, (int, float)) and x > 0 else None for x in sizes]
            vol = round(0.523 * sizes[0] * sizes[1] * sizes[2] / 1000, 3) if all(sizes) else None  # мл
            d["findings"].append({"organ": f.get("organ"), "location_raw": f.get("location_raw"),
                                  "kind": f.get("kind"), "size_a": sizes[0], "size_b": sizes[1], "size_c": sizes[2],
                                  "volume": vol, "features_raw": f.get("features_raw"),
                                  "score_system": f.get("score_system"), "score_value": f.get("score_value")})

    def _person(self, pid):
        return next((p for p in self._db.persons() if p["id"] == pid), None)

    def _rematch(self, d):
        person = self._person(d.get("person_id"))
        ua = self._db.user_aliases()
        for s in d.get("studies", []):
            s["results"] = [self._ref.match_row(r, s.get("material"), person, d.get("collected_at"),
                                                d.get("facility") or "", ua) for r in s.get("results", [])]
            self._add_derived(s, person, d, ua)
        pat = d.get("recognized_patient") or {}
        warn = []
        if person and pat.get("birth_date") and person.get("birth_date") and pat["birth_date"] != person["birth_date"]:
            warn.append(msg("warn_birth", self._settings["lang"]))
        if person and pat.get("sex") and person.get("sex") and pat["sex"] != person["sex"]:
            warn.append(msg("warn_sex", self._settings["lang"]))
        d["warnings"] = warn
        return d

    def _add_derived(self, s, person, d, ua):
        """Расчётные показатели: общий объём щитовидной железы = сумма объёмов долей (если общего нет в протоколе)."""
        # прежний расчёт пересчитываем заново, чтобы он следовал за правками объёмов долей
        s["results"] = [r for r in s.get("results", []) if not str(r.get("comment_raw") or "").startswith("derived:")]
        by_key = {r.get("analyte_key"): r for r in s["results"] if r.get("analyte_key")}
        right, left = by_key.get("imaging.thyroid.vol_right"), by_key.get("imaging.thyroid.vol_left")
        if not (right and left) or "imaging.thyroid.vol_total" in by_key:
            return
        if right.get("value_std") is None or left.get("value_std") is None:
            return
        total = round(right["value_std"] + left["value_std"], 2)
        row = {"name_raw": "Загальний об'єм (розрахунок програми)", "value_raw": str(total), "unit_raw": "мл",
               "analyte_key": "imaging.thyroid.vol_total", "key_manual": True,
               "comment_raw": "derived: vol_right + vol_left"}
        s["results"].append(self._ref.match_row(row, s.get("material"), person, d.get("collected_at"),
                                                d.get("facility") or "", ua))

    # ---------- копия для телефона ----------
    @api_method
    def export_phone(self, person_id):
        person = self._person(person_id) or {}
        name = safe_name((person.get("name") or "").split(" ")[0] or "archive")
        fname = f"medkarta-{name}-{date.today().isoformat()}.html"
        res = self._window.create_file_dialog(dialog("SAVE"), directory=str(APP_DIR) if PORTABLE else "",
                                              save_filename=fname, file_types=("HTML (*.html)",))
        if not res:
            return None
        path = Path(res[0] if isinstance(res, (list, tuple)) else res)
        if path.suffix.lower() != ".html":
            path = path.with_suffix(".html")
        path.write_text(export_phone.build(self, person_id), encoding="utf-8")
        return {"path": str(path), "size_mb": round(path.stat().st_size / 1e6, 1)}

    # ---------- наблюдения ----------
    @api_method
    def obs_state(self, person_id):
        lang = self._settings["lang"]
        return {"config": self._obs.config(lang), "timeline": self._obs.timeline(person_id),
                "correlations": self._obs.correlations(person_id, lang)}

    @api_method
    def save_obs_config(self, cfg):
        return self._obs.save_config(cfg)

    @api_method
    def save_measurement(self, person_id, m):
        return self._obs.save_measurement(person_id, m)

    @api_method
    def save_episode(self, person_id, e):
        return self._obs.save_episode(person_id, e)

    @api_method
    def get_factors(self, person_id, day):
        return self._obs.get_factors(person_id, day)

    @api_method
    def save_factors(self, person_id, day, values):
        return self._obs.save_factors(person_id, day, values)

    @api_method
    def delete_obs(self, person_id, kind, ident):
        return self._obs.delete(kind, ident, person_id)

    # ---------- шаблоны бланков ----------
    @api_method
    def form_template(self, tid):
        t = self._forms.get(tid)
        if not t:
            raise UserError("doc_not_found")
        return forms.for_ui(t, self._settings["lang"])

    @api_method
    def apply_form(self, draft, tid, values):
        t = self._forms.get(tid)
        if not t:
            raise UserError("doc_not_found")
        return self._rematch(forms.apply(t, values, draft))

    @api_method
    def preview_pages(self, rel, mime):
        return ocr.preview_data_urls(self._abs(rel), mime)

    @api_method
    def ocr_draft(self, draft):
        """Распознать текст и узнать бланк по требованию (кнопка в редакторе)."""
        f = next((x for x in draft.get("files", []) if x.get("mime") in ALLOWED.values()), None)
        if not f or not ocr.status()["available"]:
            return draft
        text = ocr.ocr_file(self._abs(f["path"]), f["mime"]) or ""
        draft["ocr_text"] = text
        tid, _ = self._forms.match(text)
        if tid and not draft.get("form_template"):
            draft["form_template"] = tid
        return draft

    @api_method
    def recognize_draft(self, draft):
        """Распознать документ через Claude по требованию (например, рукописный бланк)."""
        f = next((x for x in draft.get("files", []) if x.get("mime") in ALLOWED.values()), None)
        if not f or not self._settings.get("api_key"):
            return draft
        rec = recognize(self._abs(f["path"]), f["mime"], self._settings["api_key"], self._settings.get("model"))
        self._apply_recognized(draft, rec)
        draft["source"] = "ai"
        return self._rematch(draft)

    @api_method
    def import_template(self):
        res = self._window.create_file_dialog(dialog("OPEN"), allow_multiple=True, file_types=("JSON (*.json)",))
        added, errors = [], []
        for p in res or []:
            try:
                t = json.loads(Path(p).read_text(encoding="utf-8"))
                errs = forms.validate(t)
                if errs:
                    errors.append(f"{Path(p).name}: " + "; ".join(errs))
                    continue
                shutil.copy2(p, self._data / "templates" / f"{t['id']}.json")
                added.append(t["id"])
            except (OSError, ValueError) as e:
                errors.append(f"{Path(p).name}: {e}")
        self._forms.reload()
        return {"added": added, "errors": errors}

    @api_method
    def open_templates_dir(self):
        os.startfile(str(self._data / "templates"))  # noqa: S606
        return True

    @api_method
    def rematch_all(self):
        return {"documents": self._rematch_all()}

    @api_method
    def rematch(self, draft):
        return self._rematch(draft)

    @api_method
    def match_row(self, row, material, person_id, on_date, facility):
        return self._ref.match_row(row, material, self._person(person_id), on_date, facility or "",
                                   self._db.user_aliases())

    @api_method
    def new_draft(self, person_id):
        return self._empty_draft(person_id)

    @api_method
    def edit_draft(self, doc_id):
        d = self._db.get_document(doc_id)
        if not d:
            raise UserError("doc_not_found")
        d["new_files"] = []
        return d

    @api_method
    def panel_study(self, panel_key):
        p = self._ref.panels[panel_key]
        rows = []
        for k in p["items"]:
            a = self._ref.analytes[k]
            nm = a.get("name_" + self._settings["lang"]) or a["name_ru"]
            rows.append({"name_raw": nm, "value_raw": "", "unit_raw": a["unit_std"], "ref_raw": None,
                         "analyte_key": k, "key_manual": True})
        return {"title_raw": p.get("name_" + self._settings["lang"]) or p["name_ru"], "material": self._ref.analytes[p["items"][0]]["material"],
                "panel_key": panel_key, "results": rows}

    @api_method
    def add_files_to_draft(self, paths):
        out, dups = [], []
        for path in paths:
            f, dup = self._store_file(path)
            if dup:
                dups.append(Path(path).name)
            else:
                out.append(f)
        return {"files": out, "duplicates": dups}

    @api_method
    def save_draft(self, d, force=False):
        if not d.get("person_id"):
            raise UserError("no_person")
        if not force:
            other = self._db.find_by_order(d["person_id"], d.get("facility"), (d.get("order_no") or "").strip(), d.get("id"))
            if other:
                return {"status": "duplicate_order", "doc_id": other}
        d = self._rematch(d)
        for s in d.get("studies", []):
            s["results"] = [r for r in s.get("results", []) if (r.get("name_raw") or "").strip()
                            or str(r.get("value_raw") or "").strip()]
            for r in s["results"]:
                if r.get("key_manual") and r.get("analyte_key") and r.get("name_raw"):
                    self._db.save_user_alias(norm(r["name_raw"]), family(s.get("material")), r.get("unit") or "",
                                             r["analyte_key"])
        doc_id = self._db.save_document(d)
        return {"status": "saved", "id": doc_id}

    @api_method
    def attach_to(self, doc_id, files):
        self._db.add_files(doc_id, files)
        return True

    @api_method
    def discard_files(self, files):
        for f in files or []:
            if f.get("path") and not self._db.path_in_use(f["path"]):
                try:
                    self._abs(f["path"]).unlink()
                except OSError:
                    pass
        return True

    # ---------- показатели ----------
    @api_method
    def indicators(self, person_id):
        series = {}
        for r in self._db.series_rows(person_id):
            if r["analyte_key"]:
                a = self._ref.analytes.get(r["analyte_key"], {})
                sid, name = r["analyte_key"], a.get("name_" + self._settings["lang"]) or a.get("name_ru") or r["name_raw"]
                val, unit = r["value_std"], unit_label(r["unit_std"], self._settings["lang"])
            else:
                sid, name, val, unit = "raw:" + norm(r["name_raw"]) + "|" + (r["unit"] or ""), r["name_raw"], r["value"], r["unit_raw"]
            if val is None:
                continue
            s = series.setdefault(sid, {"id": sid, "name": name, "unit": unit, "matched": bool(r["analyte_key"]),
                                        "points": []})
            s["points"].append({"date": r["collected_at"], "value": val, "op": r["value_op"], "flag": r["flag"],
                                "low": r["ref_low"], "high": r["ref_high"], "method_changed": r["method_changed"],
                                "doc_id": r["doc_id"], "facility": r["facility"]})
        out = sorted(series.values(), key=lambda s: (not s["matched"], -len(s["points"]), s["name"]))
        return self._obs.series(person_id, self._settings["lang"]) + out


def main():
    api = Api()
    title = {"ru": "Медкарта", "uk": "Медкарта", "en": "Medkarta"}[api._settings["lang"]]
    window = webview.create_window(title, str(BASE / "ui" / "index.html"), js_api=api,
                                   width=1240, height=860, min_size=(960, 640))
    api._window = window
    # private_mode: окно не сохраняет кэш, cookies и историю на диске компьютера
    webview.start(debug="--debug" in sys.argv, private_mode=True)


if __name__ == "__main__":
    main()
