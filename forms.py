"""Шаблоны бумажных бланков: загрузка, узнавание по печатному тексту (OCR), перенос заполненных полей в документ.

Шаблон — JSON-файл. Встроенные лежат в папке templates рядом с программой, личные — в папке templates
внутри архива. Личный шаблон с тем же id заменяет встроенный. Формат описан в templates/README.md.
"""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

FIELD_TYPES = {"number", "dims", "choice", "text", "findings", "section"}


def _norm(s):
    s = str(s or "").lower().replace("’", "'").replace("`", "'").replace("ʼ", "'")
    s = re.sub(r"[^\w'%/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fuzzy_contains(hay_norm, needle):
    """Насколько фраза встречается в тексте с учётом ошибок OCR: 0..1."""
    n = _norm(needle)
    if not n:
        return 0.0
    if n in hay_norm:
        return 1.0
    words, k = hay_norm.split(), len(n.split())
    best = 0.0
    for size in {k, k + 1, max(1, k - 1)}:
        for i in range(0, max(1, len(words) - size + 1)):
            r = SequenceMatcher(None, " ".join(words[i:i + size]), n).ratio()
            if r > best:
                best = r
                if best > 0.95:
                    return best
    return best


def validate(t):
    errors = []
    for key in ("id", "title", "fields"):
        if key not in t:
            errors.append(f"нет поля «{key}»")
    ids = set()
    for f in t.get("fields", []):
        if f.get("type") not in FIELD_TYPES:
            errors.append(f"поле {f.get('id')}: неизвестный тип {f.get('type')}")
        if f.get("type") != "section":
            if not f.get("id"):
                errors.append("поле без id")
            elif f["id"] in ids:
                errors.append(f"повтор id {f['id']}")
            ids.add(f.get("id"))
    return errors


class FormLibrary:
    def __init__(self, builtin_dir, user_dir):
        self.builtin_dir, self.user_dir = Path(builtin_dir), Path(user_dir)
        self.reload()

    def reload(self):
        self.templates, self.errors = {}, []
        for origin, folder in (("builtin", self.builtin_dir), ("user", self.user_dir)):
            if not folder.is_dir():
                continue
            for p in sorted(folder.glob("*.json")):
                try:
                    t = json.loads(p.read_text(encoding="utf-8"))
                    errs = validate(t)
                    if errs:
                        self.errors.append(f"{p.name}: " + "; ".join(errs))
                        continue
                    t["_origin"], t["_file"] = origin, p.name
                    self.templates[t["id"]] = t      # личный шаблон с тем же id заменяет встроенный
                except (OSError, ValueError) as e:
                    self.errors.append(f"{p.name}: {e}")

    def summary(self, lang):
        out = []
        for t in self.templates.values():
            out.append({"id": t["id"], "title": _tr(t["title"], lang), "facility": t.get("facility", ""),
                        "kind": t.get("kind", "imaging"), "origin": t["_origin"], "file": t["_file"],
                        "fields": sum(1 for f in t["fields"] if f["type"] != "section"),
                        "author": t.get("author", ""), "version": t.get("version", 1)})
        return sorted(out, key=lambda x: (x["origin"] != "user", x["title"]))

    def get(self, tid):
        return self.templates.get(tid)

    def match(self, text):
        """Узнать бланк по распознанному тексту. -> (template_id, score) или (None, 0)."""
        hay = _norm(text)
        if len(hay) < 30:
            return None, 0.0
        best, best_score = None, 0.0
        for t in self.templates.values():
            m = t.get("match") or {}
            req = m.get("all", [])
            if not req:
                continue
            scores = [fuzzy_contains(hay, k) for k in req]
            hit = sum(1 for s in scores if s >= 0.8) / len(scores)
            opt = m.get("any", [])
            if opt:
                hit = 0.8 * hit + 0.2 * (sum(1 for k in opt if fuzzy_contains(hay, k) >= 0.8) / len(opt))
            if any(fuzzy_contains(hay, k) >= 0.85 for k in m.get("none", [])):
                hit *= 0.4
            if hit > best_score:
                best, best_score = t["id"], hit
        return (best, best_score) if best_score >= 0.6 else (None, best_score)


def _tr(v, lang):
    if isinstance(v, dict):
        return v.get(lang) or v.get("uk") or v.get("ru") or v.get("en") or next(iter(v.values()), "")
    return v or ""


def for_ui(t, lang):
    """Шаблон для формы ввода: подписи на выбранном языке (если в шаблоне есть перевод)."""
    fields = []
    for f in t["fields"]:
        g = {k: v for k, v in f.items() if k not in ("label", "options")}
        g["label"] = _tr(f.get("label", f.get("id", "")), lang)
        if "options" in f:
            g["options"] = [_tr(o, lang) if isinstance(o, dict) else o for o in f["options"]]
        fields.append(g)
    return {"id": t["id"], "title": _tr(t["title"], lang), "facility": t.get("facility", ""),
            "kind": t.get("kind", "imaging"), "fields": fields}


def _num(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _fmt(x):
    return ("%.2f" % x).rstrip("0").rstrip(".")


def apply(t, values, draft):
    """Перенести заполненные поля в черновик документа. Исследование этого шаблона заменяется целиком."""
    study_title = t.get("study_title") or _tr(t["title"], "uk")
    results, findings = [], []
    v = values or {}

    def row(name, value, unit=None, key=None, ref=None):
        r = {"name_raw": name, "value_raw": value, "unit_raw": unit, "ref_raw": ref, "ref_selected": None,
             "flag_raw": None, "comment_raw": "form:" + t["id"], "method_changed": False, "codes": []}
        if key:
            r.update(analyte_key=key, key_manual=True)
        results.append(r)

    for f in t["fields"]:
        ft, fid = f["type"], f.get("id")
        name = f.get("name_raw") or _tr(f.get("label", fid), "uk")
        val = v.get(fid)
        if ft == "section" or val in (None, "", [], {}):
            if ft == "number" and f.get("calc") and val in (None, ""):
                val = _calc(f["calc"], v)
                if val is None:
                    continue
            else:
                continue
        if ft == "number":
            n = _num(val)
            if n is None:
                continue
            row(name, _fmt(n), f.get("unit"), f.get("analyte"), f.get("ref"))
        elif ft == "dims":
            parts = [_num(x) for x in (val if isinstance(val, list) else str(val).replace("х", "x").split("x"))]
            parts = [p for p in parts if p is not None]
            if not parts:
                continue
            keys = f.get("analytes") or []
            names = f.get("part_names") or []
            for i, p in enumerate(parts):
                if i < len(keys) and keys[i]:
                    row(names[i] if i < len(names) else f"{name} {i + 1}", _fmt(p), f.get("unit"), keys[i])
            if len(parts) > len([k for k in keys if k]):
                row(name, " × ".join(_fmt(p) for p in parts) + (f" {f['unit']}" if f.get("unit") else ""))
        elif ft == "choice":
            text = ", ".join(val) if isinstance(val, list) else str(val)
            target = f.get("target")
            if target:
                draft[target] = text
            else:
                row(name, text)
        elif ft == "text":
            target = f.get("target")
            if target in ("summary", "notes", "doctor", "title"):
                draft[target] = str(val).strip()
            elif target == "collected_at":
                draft["collected_at"] = _date(val) or draft.get("collected_at")
            else:
                row(name, str(val).strip())
        elif ft == "findings":
            for item in val if isinstance(val, list) else []:
                sizes = [_num(item.get(k)) for k in ("size_a", "size_b", "size_c")]
                if not any(item.get(k) for k in ("location_raw", "kind", "features_raw")) and not any(sizes):
                    continue
                mm = f.get("size_unit", "mm") == "cm"
                sizes = [round(s * 10, 2) if (s is not None and mm) else s for s in sizes]
                vol = round(0.523 * sizes[0] * sizes[1] * sizes[2] / 1000, 3) if all(sizes) else None
                findings.append({"organ": item.get("organ") or f.get("organ", ""), "location_raw": item.get("location_raw", ""),
                                 "kind": item.get("kind", ""), "size_a": sizes[0], "size_b": sizes[1], "size_c": sizes[2],
                                 "volume": vol, "features_raw": item.get("features_raw", ""),
                                 "score_system": item.get("score_system"), "score_value": item.get("score_value")})
    tag = "form:" + t["id"]
    draft["studies"] = [s for s in draft.get("studies", []) if s.get("form_template") != t["id"]]
    if results:
        draft["studies"].append({"title_raw": study_title, "material": t.get("material", "imaging"), "method_raw": None,
                                 "section_raw": None, "comment_raw": None, "results": results, "form_template": t["id"]})
    draft["findings"] = [x for x in draft.get("findings", []) if x.get("comment_raw") != tag] + [dict(x, comment_raw=tag) for x in findings]
    draft["kind"] = t.get("kind", draft.get("kind", "imaging"))
    if not draft.get("title") or draft.get("title") == draft.get("_file_title"):
        draft["title"] = _tr(t["title"], "uk")
    if t.get("facility") and not draft.get("facility"):
        draft["facility"] = t["facility"]
    draft["form_template"], draft["form_values"] = t["id"], v
    return draft


def _calc(expr, v):
    """Расчётные поля: brunn(dims_id) — объём доли щитовидной железы по Брунну, см³."""
    m = re.match(r"brunn\((\w+)\)", expr or "")
    if m:
        d = v.get(m.group(1))
        parts = [_num(x) for x in (d if isinstance(d, list) else [])]
        if len(parts) == 3 and all(p is not None for p in parts):
            return round(0.479 * parts[0] * parts[1] * parts[2], 1)
    return None


def _date(s):
    s = str(s or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})$", s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None
