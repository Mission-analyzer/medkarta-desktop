"""Справочник и сопоставление строк бланка с показателями.

Правила описаны в документе «Медкарта — модель данных», раздел «Сопоставление при вводе».
"""
import json
import re
import unicodedata
from datetime import date

CYR_LOOKALIKE = str.maketrans("АВЕКМНОРСТХІУ", "ABEKMHOPCTXIY")
APOSTROPHES = re.compile(r"[`’ʼ‘´′]")
MICRO = str.maketrans({"μ": "µ"})


def norm(s):
    """Нормализованная строка для сравнения названий."""
    s = unicodedata.normalize("NFC", str(s or ""))
    s = APOSTROPHES.sub("'", s).lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s).strip(" .;:")
    return s


def abbr_norm(s):
    """Аббревиатура: верхний регистр, кириллические двойники латиницы -> латиница."""
    s = re.sub(r"\s+", "", str(s or "")).upper().translate(CYR_LOOKALIKE)
    return s.strip(".")


def family(material):
    return (material or "").split("_")[0] or None


ABBR_TOKEN = re.compile(r"^[A-Za-zА-ЯІЇЄҐа-яіїєґ0-9#%\-\.]{1,12}$")


def is_abbr(tok):
    tok = tok.strip()
    if not ABBR_TOKEN.match(tok):
        return False
    if re.search(r"[A-Za-z]", tok):
        return True
    return tok.upper() == tok and len(tok) <= 6  # «ТТГ», «ШОЕ»


def split_name(raw, section_prefixes):
    """-> (section, rest, base, abbrs)"""
    raw = unicodedata.normalize("NFC", str(raw or "")).strip()
    section = None
    if ":" in raw:
        head, tail = raw.split(":", 1)
        if norm(head) in section_prefixes:
            section, raw = head.strip(), tail.strip()
    rest = raw
    abbrs = []
    m = re.search(r"/\s*([A-Za-z0-9\-#%]{1,12})\s*$", rest)
    if m:
        abbrs.append(m.group(1))
        rest = rest[: m.start()].strip()
    base = rest
    for grp in re.findall(r"\(([^()]*)\)", rest):
        toks = [t.strip() for t in grp.split(",") if t.strip()]
        if toks and all(is_abbr(t) for t in toks):
            abbrs.extend(toks)
            base = base.replace(f"({grp})", " ")
    return section, rest, re.sub(r"\s+", " ", base).strip(), abbrs


NUM = r"-?\d+(?:[.,]\d+)?"


def to_float(s):
    return float(str(s).replace(",", "."))


def parse_value(raw):
    """-> dict(result_type, value, value_op, value_text, star)"""
    s = str(raw if raw is not None else "").strip()
    star = s.endswith("*")
    s = s.rstrip("*").strip()
    m = re.match(rf"^(<=|>=|≤|≥|<|>)?\s*({NUM})$", s)
    if m:
        op = {"≤": "<=", "≥": ">="}.get(m.group(1), m.group(1))
        return {"result_type": "numeric", "value": to_float(m.group(2)), "value_op": op, "value_text": None, "star": star}
    m = re.match(r"^1\s*:\s*(\d+)$", s)
    if m:
        return {"result_type": "titer", "value": float(m.group(1)), "value_op": None, "value_text": s, "star": star}
    if not s:
        return {"result_type": "text", "value": None, "value_op": None, "value_text": None, "star": star}
    return {"result_type": "qualitative" if len(s) <= 60 else "text", "value": None, "value_op": None,
            "value_text": s, "star": star}


def parse_ref(s):
    """Норма одной строкой -> (low, high) или None. «Чоловіки: 4.0 - 5.0», «<21», «≤5.0», «до 20»."""
    if not s:
        return None
    s = str(s).strip()
    if "\n" in s:
        return None  # многострочную норму программа не угадывает
    if ":" in s:
        s = s.rsplit(":", 1)[1]
    s = s.strip().replace("–", "-").replace("—", "-")
    m = re.match(rf"^({NUM})\s*-\s*({NUM})", s)
    if m:
        return to_float(m.group(1)), to_float(m.group(2))
    m = re.match(rf"^(?:<=|≤|<|до|менее|менше)\s*({NUM})", s, re.I)
    if m:
        return None, to_float(m.group(1))
    m = re.match(rf"^(?:>=|≥|>|от|від|более|більше)\s*({NUM})", s, re.I)
    if m:
        return to_float(m.group(1)), None
    return None


def compute_flag(v, op, low, high):
    if v is None or (low is None and high is None):
        return None
    if op in ("<", "<="):
        if low is not None and v <= low:
            return None
        return "high" if high is not None and v > high else "normal"
    if op in (">", ">="):
        if high is not None and v >= high:
            return None
        return "low" if low is not None and v < low else "normal"
    if low is not None and v < low:
        return "low"
    if high is not None and v > high:
        return "high"
    return "normal"


def age_days(birth, on):
    try:
        b = date.fromisoformat(birth)
        d = date.fromisoformat(on)
        return (d - b).days
    except (TypeError, ValueError):
        return None


class Reference:
    def __init__(self, path):
        data = json.loads(open(path, encoding="utf-8").read())
        self.data = data
        self.analytes = {a["key"]: a for a in data["analytes"]}
        self.units = {k.translate(MICRO): v for k, v in data["units"].items()}
        self.section_prefixes = {norm(p) for p in data.get("section_prefixes", [])}
        self.conversions = {(c["key"], c["from"]): c["factor"] for c in data["conversions"]}
        self.norms = data["norms"]
        self.panels = {p["key"]: p for p in data["panels"]}
        self.name_index, self.abbr_index = {}, {}
        for a in data["analytes"]:
            for n in (a["name_ru"], a["name_uk"]):
                self.name_index.setdefault(norm(n), []).append((a["key"], a["unit_std"]))
            for ab in a["abbr"]:
                self.abbr_index.setdefault(abbr_norm(ab), set()).add(a["key"])
        for al in data["aliases"]:
            self.name_index.setdefault(norm(al["alias"]), []).append((al["key"], al["unit_hint"]))
            _, _, _, abbrs = split_name(al["alias"], self.section_prefixes)
            for ab in abbrs:
                self.abbr_index.setdefault(abbr_norm(ab), set()).add(al["key"])

    # ---------- единицы ----------
    def unit_code(self, raw):
        if not raw:
            return ""
        s = unicodedata.normalize("NFC", str(raw)).strip().translate(MICRO)
        s = re.sub(r"\s+", " ", s)
        for cand in (s, s.rstrip("."), s.replace(" ", "")):
            if cand in self.units:
                return self.units[cand]
        hits = {v for k, v in self.units.items() if k.lower() == s.lower()}
        if len(hits) == 1:  # регистр не важен, только если он не меняет смысл (Г/л и г/л)
            return hits.pop()
        return s

    # ---------- показатель ----------
    def _pick(self, cands, unit_code, material):
        keys = list(dict.fromkeys(k for k, _ in cands))
        if len(keys) <= 1:
            return keys[0] if keys else None
        by_hint = list(dict.fromkeys(k for k, h in cands if h and h == unit_code))
        if len(by_hint) == 1:
            return by_hint[0]
        by_std = [k for k in keys if self.analytes[k]["unit_std"] == unit_code]
        if len(by_std) == 1:
            return by_std[0]
        if unit_code == "%":
            pct = [k for k in keys if k.endswith("_pct")]
            if len(pct) == 1:
                return pct[0]
        return None  # неоднозначно

    def match_name(self, name_raw, unit_code, material, user_aliases=None):
        """-> (key, via, review_code, section, abbrs). via: user_alias | name | abbr"""
        section, rest, base, abbrs = split_name(name_raw, self.section_prefixes)
        fam = family(material)
        if user_aliases:
            k = user_aliases.get((norm(name_raw), fam, unit_code)) or user_aliases.get((norm(name_raw), fam, ""))
            if k:
                return k, "user_alias", None, section, abbrs
        def same_family(k):  # показатель крови не может прийти из анализа мочи
            return not fam or family(self.analytes[k]["material"]) == fam

        name_cands = []
        for n in (name_raw, rest, base):
            name_cands += [c for c in self.name_index.get(norm(n), []) if same_family(c[0])]
        name_key = self._pick(name_cands, unit_code, material) if name_cands else None
        abbr_keys = set()
        for ab in abbrs:
            abbr_keys |= {k for k in self.abbr_index.get(abbr_norm(ab), set()) if same_family(k)}
        abbr_key = self._pick([(k, "") for k in abbr_keys], unit_code, material) if abbr_keys else None
        if name_key and abbr_keys and name_key not in abbr_keys:
            return name_key, "name", "abbr_conflict", section, abbrs
        if name_key:
            return name_key, "name", None, section, abbrs
        if abbr_key:
            return abbr_key, "abbr", None, section, abbrs
        if name_cands or abbr_keys:
            return None, None, "ambiguous", section, abbrs
        return None, None, None, section, abbrs

    # ---------- нормы ----------
    def ref_norm(self, key, sex, days, facility=""):
        rows = [n for n in self.norms if n["key"] == key and not n["condition"]
                and n["sex"] in ((sex or "any"), "any")
                and (days is None or n["from_days"] is None or n["from_days"] <= days)
                and (days is None or n["to_days"] is None or days < n["to_days"])]
        if days is None:
            rows = [n for n in rows if n["to_days"] is None]
        if not rows:
            return None
        fac = norm(facility)

        def lab_match(n):
            first = norm(n["source"]).split(",")[0].split(" ")[0]
            return 0 if fac and first and first in fac else 1

        def span(n):  # узкая возрастная норма (60–69 лет) точнее общей «для взрослых»
            if n["from_days"] is None and n["to_days"] is None:
                return 10 ** 9
            return (n["to_days"] or 200 * 366) - (n["from_days"] or 0)

        rows.sort(key=lambda n: (lab_match(n), 0 if n["sex"] == sex else 1, span(n)))
        return rows[0]

    def plausible(self, key, low, high):
        if low is not None and high is not None and low >= high:
            return False
        typical = [n for n in self.norms if n["key"] == key and n["high"]]
        if typical and high is not None and high > 0:
            th = sorted(n["high"] for n in typical)[len(typical) // 2]
            if not (th / 20 <= high <= th * 20):
                return False
        return True

    # ---------- строка целиком ----------
    def match_row(self, row, material, person, on_date, facility="", user_aliases=None):
        r = dict(row)
        unit_code = self.unit_code(r.get("unit_raw"))
        pv = parse_value(r.get("value_raw"))
        r.update(result_type=pv["result_type"], value=pv["value"], value_op=pv["value_op"], value_text=pv["value_text"],
                 unit=unit_code)
        flag_raw = str(r.get("flag_raw") or "").strip()
        r["flag_lab"] = "*" if pv["star"] else (flag_raw or None)
        reasons = []
        key = r.get("analyte_key") if r.get("key_manual") else None
        if not key:
            key, via, reason, section, abbrs = self.match_name(r.get("name_raw"), unit_code, material, user_aliases)
            r["match_via"] = via
            if reason:
                reasons.append(reason)
            if section and not r.get("section_raw"):
                r["section_raw"] = section
            r["abbr_raw"] = ", ".join(abbrs) or None
        else:
            r["match_via"] = "manual"
        r["analyte_key"] = key
        a = self.analytes.get(key) if key else None
        # пересчёт в основную единицу
        factor = None
        if a:
            std = a["unit_std"]
            if unit_code == std or not std:
                factor = 1.0
            elif (key, unit_code) in self.conversions:
                factor = self.conversions[(key, unit_code)]
            elif r["result_type"] == "numeric":
                reasons.append(f"unit_noconv|{r.get('unit_raw') or '—'}|{std}")
        else:
            factor = 1.0
        r["unit_std"] = a["unit_std"] if a else unit_code
        r["value_std"] = r["value"] * factor if (r["value"] is not None and factor is not None) else None
        # норма: бланк -> справочник
        r["ref_low"] = r["ref_high"] = r["ref_source"] = None
        pr = parse_ref(r.get("ref_selected")) or parse_ref(r.get("ref_raw"))
        if pr and factor is not None:
            low, high = (None if pr[0] is None else pr[0] * factor), (None if pr[1] is None else pr[1] * factor)
            if not key or self.plausible(key, low, high):
                r.update(ref_low=low, ref_high=high, ref_source="blank")
            else:
                reasons.append("ref_implausible")
        if r["ref_source"] is None and key and r["result_type"] == "numeric":
            days = age_days((person or {}).get("birth_date"), on_date)
            n = self.ref_norm(key, (person or {}).get("sex"), days, facility)
            if n:
                r.update(ref_low=n["low"], ref_high=n["high"], ref_source="ref|" + n["source"])
        r["flag"] = compute_flag(r["value_std"], r["value_op"], r["ref_low"], r["ref_high"]) \
            if r["result_type"] == "numeric" else None
        r["needs_review"] = 1 if reasons else 0
        r["review_reason"] = ";".join(reasons) or None  # коды причин; текст — в интерфейсе на языке пользователя
        return r


UNIT_LABELS = {
    "g/L": {"uk": "г/л", "ru": "г/л", "en": "g/L"}, "g/dL": {"uk": "г/дл", "ru": "г/дл", "en": "g/dL"},
    "10^9/L": {"uk": "×10⁹/л", "ru": "×10⁹/л", "en": "×10⁹/L"}, "10^12/L": {"uk": "×10¹²/л", "ru": "×10¹²/л", "en": "×10¹²/L"},
    "umol/L": {"uk": "мкмоль/л", "ru": "мкмоль/л", "en": "µmol/L"}, "mmol/L": {"uk": "ммоль/л", "ru": "ммоль/л", "en": "mmol/L"},
    "U/L": {"uk": "Од/л", "ru": "Ед/л", "en": "U/L"}, "IU/L": {"uk": "МО/л", "ru": "МЕ/л", "en": "IU/L"},
    "mIU/L": {"uk": "мМО/л", "ru": "мМЕ/л", "en": "mIU/L"}, "IU/mL": {"uk": "МО/мл", "ru": "МЕ/мл", "en": "IU/mL"},
    "fL": {"uk": "фл", "ru": "фл", "en": "fL"}, "pg": {"uk": "пг", "ru": "пг", "en": "pg"}, "mm/h": {"uk": "мм/год", "ru": "мм/ч", "en": "mm/h"},
    "cm": {"uk": "см", "ru": "см", "en": "cm"}, "mm": {"uk": "мм", "ru": "мм", "en": "mm"}, "mL": {"uk": "мл", "ru": "мл", "en": "mL"},
    "cm/s": {"uk": "см/с", "ru": "см/с", "en": "cm/s"}, "pmol/L": {"uk": "пмоль/л", "ru": "пмоль/л", "en": "pmol/L"},
    "nmol/L": {"uk": "нмоль/л", "ru": "нмоль/л", "en": "nmol/L"}, "ng/mL": {"uk": "нг/мл", "ru": "нг/мл", "en": "ng/mL"},
    "ng/dL": {"uk": "нг/дл", "ru": "нг/дл", "en": "ng/dL"}, "pg/mL": {"uk": "пг/мл", "ru": "пг/мл", "en": "pg/mL"},
    "g/mL": {"uk": "г/мл", "ru": "г/мл", "en": "g/mL"}, "mg/dL": {"uk": "мг/дл", "ru": "мг/дл", "en": "mg/dL"},
    "cells/uL": {"uk": "кл/мкл", "ru": "кл/мкл", "en": "cells/µL"}, "/uL": {"uk": "/мкл", "ru": "/мкл", "en": "/µL"},
    "/hpf": {"uk": "у п/з", "ru": "в п/зр", "en": "/hpf"},
    "mL/min/1.73m2": {"uk": "мл/хв/1,73 м²", "ru": "мл/мин/1,73 м²", "en": "mL/min/1.73 m²"},
    "mg/L": {"uk": "мг/л", "ru": "мг/л", "en": "mg/L"}, "mg/mmol": {"uk": "мг/ммоль", "ru": "мг/ммоль", "en": "mg/mmol"},
    "s": {"uk": "с", "ru": "с", "en": "s"}, "ug/mL": {"uk": "мкг/мл", "ru": "мкг/мл", "en": "µg/mL"}, "ug/dL": {"uk": "мкг/дл", "ru": "мкг/дл", "en": "µg/dL"},
}


def unit_label(code, lang):
    """Код единицы справочника -> подпись для человека на языке интерфейса."""
    u = UNIT_LABELS.get(code or "")
    return (u.get(lang) or u["en"]) if u else (code or "")

