"""Локальный разбор текстовых PDF лабораторий — без интернета и без ИИ.

parse_pdf(path, person) -> dict в том же формате, что возвращает распознавание (recognize.py), или
{"kind": "report", ...} для сопроводительного отчёта, или None, если бланк не узнан.
Сопоставление со справочником потом делает reference.py, как и для любого другого источника.

Поддерживаются: «Сінево» (таблица результатов и отчёт-приложение), CSD LAB (таблица результатов
и патогистология), «Діла» (по текстовой структуре; бета).
"""
import re
from datetime import date

try:
    import pdfplumber
except ImportError:  # программа работает и без него — просто без локального разбора
    pdfplumber = None

NUM = r"\d+(?:[.,]\d+)?"
DATE_DOT = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
DATE_DASH = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


def _iso(m):
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _bold(w):
    return "Bold" in w.get("fontname", "")


def _italic(w):
    f = w.get("fontname", "")
    return "Italic" in f or "Oblique" in f


def _join(ws):
    """Слова одной строки -> текст; близко стоящие слова (мкмоль / л) склеиваются без пробела."""
    out, prev = "", None
    for w in sorted(ws, key=lambda w: w["x0"]):
        if prev is not None and w["x0"] - prev["x1"] > 1.0:
            out += " "
        out += w["text"]
        prev = w
    return out.strip()


def _bands(ws, tol=2.6):
    """Группировка слов по строкам (по вертикали)."""
    bands = []
    for w in sorted(ws, key=lambda w: w["top"]):
        if bands and w["top"] - bands[-1]["top"] <= tol:
            bands[-1]["ws"].append(w)
        else:
            bands.append({"top": w["top"], "ws": [w]})
    return bands


def _page_words(page):
    return [w for w in page.extract_words(extra_attrs=["fontname"], x_tolerance=1.5)
            if 0 <= w["top"] <= page.height and 0 <= w["x0"] <= page.width]


def _page_text(page):
    return page.within_bbox((0, 0, page.width, page.height)).extract_text() or ""


def _age_years(birth, on):
    try:
        b, d = date.fromisoformat(birth), date.fromisoformat(on)
        return (d - b).days / 365.25
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- выбор строки нормы
VALUE_RE = re.compile(rf"(?:(<=|>=|≤|≥|<|>|до)\s*)?({NUM})(?:\s*[-–—]\s*({NUM}))?")


def _value_part(line):
    """Последнее «значение нормы» в строке: диапазон a-b или число со знаком. -> (match, text) или (None, None)."""
    best = None
    for m in VALUE_RE.finditer(line):
        if m.group(3) or m.group(1):
            best = m
    if not best:
        return None, None
    return best, best.group(0).strip()


def _label_info(label):
    """Пол и возраст из подписи строки нормы."""
    low = label.lower()
    info = {"sex": None, "age": None, "adult": None, "pregnant": "вагітн" in low or "беремен" in low}
    if re.search(r"чолов|мужч|\(ч\)", low):
        info["sex"] = "m"
    elif re.search(r"жін|женщ|\(ж\)", low):
        info["sex"] = "f"
    if re.search(r"новонар|дн\.|дні|днів|тижн|годин|міс\.|місяц|month", low):
        info["age"] = (0, 1)
    m = re.search(r"(\d+)\s*[-–]\s*<?\s*(\d+)\s*(?:рок|рік|рoк|рок)", low) or re.search(r"\((\d+)\s*[-–]\s*(\d+)\s*рок", low)
    if m:
        info["age"] = (float(m.group(1)), float(m.group(2)))
    else:
        m = re.search(r"(?:<|до)\s*(\d+)\s*(?:рок|рік)", low)
        if m:
            info["age"] = (0, float(m.group(1)))
        m2 = re.search(r"(?:>|≥|старш\w*|від|понад)\s*(\d+)\s*(?:рок|рік)", low)
        if m2 and not m:
            info["age"] = (float(m2.group(1)), 200)
    if "дорослі" in low or "дорослых" in low or "взросл" in low:
        info["adult"] = True
    if "діти" in low or "дети" in low:
        info["adult"] = False
    return info


def select_ref(ref_raw, sex, age):
    """Строка нормы для конкретного человека из многострочной нормы бланка. -> текст значения или None."""
    if not ref_raw:
        return None
    lines = [l.strip() for l in str(ref_raw).split("\n") if l.strip()]
    ctx = {"sex": None, "age": None, "adult": None, "pregnant": False}
    cands = []
    for line in lines:
        after = line.rsplit(":", 1)[1] if ":" in line else ""
        if after.strip() and re.search(r"\d", line.rsplit(":", 1)[0]) and not re.search(r"\d", after):
            continue                               # шкала интерпретации («7.0 - 7.5: в нормі…»), а не норма
        m, val = _value_part(line)
        if not m or line.rstrip().endswith(":"):   # «Дорослі ≥19 років:» — заголовок группы, а не норма
            info = _label_info(line)
            ctx = {"sex": info["sex"], "age": info["age"], "adult": info["adult"], "pregnant": info["pregnant"]}
            continue
        label = (line[:m.start()] + " " + line[m.end():]).strip()
        info = _label_info(label)
        own_group = info["adult"] is not None             # «Дорослі: …» не наследует возраст из «Новонароджені:»
        merged = {"sex": info["sex"] or ctx["sex"], "age": info["age"] or (None if own_group else ctx["age"]),
                  "adult": info["adult"] if info["adult"] is not None else ctx["adult"],
                  "pregnant": info["pregnant"] or ctx["pregnant"]}
        if merged["adult"] is False and merged["age"] and merged["age"][1] > 18:
            merged["age"] = (merged["age"][0], 18)
        cands.append((merged, val, bool(label.strip(" :-–*")), line))
    if not cands:
        return None
    if len(cands) == 1 and not cands[0][2]:
        return cands[0][1]
    best, best_score = None, -1
    for info, val, has_label, line in cands:
        if info["pregnant"]:
            continue
        if info["sex"] and sex and info["sex"] != sex:
            continue
        if age is not None:
            if info["age"] and not (info["age"][0] <= age < info["age"][1]):
                continue
            if not info["age"]:
                if info["adult"] is True and age < 18:
                    continue
                if info["adult"] is False and age >= 18:
                    continue
        score = (2 if info["sex"] else 0) + (2 if info["age"] else 0) + (1 if info["adult"] is not None else 0)
        if score > best_score:
            best, best_score = val, score
    return best


# ---------------------------------------------------------------- определение бланка
def detect(text):
    t = text
    if "Патогістологічне дослідження" in t:
        return "csd_histology"
    if "Інформація про клієнта" in t and "Штрих-код" in t:   # красивый отчёт «Сінево» (логотип — картинка, текста нет)
        return "synevo_report"
    if ("Сінево (Synevo)" in t or "СІНЕВО" in t) and "Показник" in t and "Результат" in t:
        return "synevo"
    if ("CSD LAB" in t or "СІ EC ДІ ЛАБ" in t or "СІ ЕС ДІ ЛАБ" in t) and "ПОКАЗНИК" in t:
        return "csd"
    if "Первинна проба" in t and ("символом * позначаються" in t or "Назва дослідження" in t):
        return "dila"
    return None


def _material(title):
    t = (title or "").lower()
    if "сеч" in t or "моч" in t:
        return "urine"
    if "копрограм" in t or "кал" in t:
        return "stool"
    return "blood_venous"


def _columns(words):
    """Границы колонок по заголовку таблицы. -> (header_top, value_min, unit_min, ref_min) или None."""
    def find(names, top=None):
        for w in words:
            if w["text"].rstrip(".").lower() in names and (top is None or abs(w["top"] - top) < 4):
                return w
        return None
    # заголовок таблицы — строка, где рядом стоят «Результат» и «Референтний»; слово «результат» бывает и в тексте выше
    res = unit = ref = None
    for w in words:
        if w["text"].rstrip(".").lower() == "результат":
            ref = find(("референтний", "референтні"), w["top"])
            unit = find(("од", "од."), w["top"])
            if ref and unit:
                res = w
                break
    if not res:
        return None
    return (res["top"], res["x0"] - 25, unit["x0"] - 14, min(unit["x0"] + 28, ref["x0"] - 20))


def _footer_top(bands, markers):
    for b in bands:
        t = _join(b["ws"])
        if any(t.startswith(m) for m in markers):
            return b["top"]
    return 10 ** 6


def name_text_is_method(name_ws):
    return _join(name_ws).startswith("Методи")


# ---------------------------------------------------------------- «Сінево»
def parse_synevo(pdf, person):
    first = _page_text(pdf.pages[0])
    rec = {"kind": "lab", "facility": "Сінево (Synevo)", "studies": [], "findings": [], "patient": {}}
    m = re.search(r"Лабораторія\s+(\S+)", first)
    if m:
        rec["facility"] += ", Лабораторія " + m.group(1)
    m = re.search(r"Дата замовлення\s*:\s*(\d{2}\.\d{2}\.\d{4})", first)
    rec["collected_at"] = _iso(DATE_DOT.search(m.group(1))) if m else None
    m = re.search(r"Дата народження\s*:\s*(\d{2}\.\d{2}\.\d{4})", first)
    rec["patient"]["birth_date"] = _iso(DATE_DOT.search(m.group(1))) if m else None
    m = re.search(r"Стать\s*:\s*(Чоловіча|Жіноча)", first)
    rec["patient"]["sex"] = {"Чоловіча": "m", "Жіноча": "f"}.get(m.group(1)) if m else None
    m = re.search(r"\b(\d{10})\b", first)
    rec["order_no"] = m.group(1) if m else None
    m = re.search(r"Дата друку\s*:\s*(\d{2}\.\d{2}\.\d{4})", first)
    rec["reported_at"] = _iso(DATE_DOT.search(m.group(1))) if m else None

    sex = rec["patient"]["sex"] or (person or {}).get("sex")
    age = _age_years(rec["patient"]["birth_date"] or (person or {}).get("birth_date"), rec["collected_at"])
    study, result, state = None, None, None
    last_name_top, last_kind = -100, None

    def new_study(title):
        nonlocal study
        title = title.replace("Індикатор зони підвищеної уваги", "").strip()
        if study and study["title_raw"].startswith(title):
            return                               # заголовок повторён на новой странице
        if study and not study["results"] and not study.get("method_raw"):
            rec["studies"].remove(study)        # заголовок-раздел без показателей («Загальна клініка»)
        study = {"title_raw": title, "material": _material(title), "method_raw": None, "results": []}
        rec["studies"].append(study)

    for page in pdf.pages:
        words = _page_words(page)
        cols = _columns(words)
        if not cols:
            continue
        htop, vmin, umin, rmin = cols
        bands = [b for b in _bands([w for w in words if w["top"] > htop + 6])]
        ftop = _footer_top(bands, ("Результати лабораторних досліджень", "Примітка"))
        frames = [(l["top"], l["bottom"]) for l in page.lines
                  if abs(l["x0"] - l["x1"]) < 1 and vmin - 6 <= l["x0"] <= vmin + 12 and 8 < l["bottom"] - l["top"] < 60]
        last_name_top = -100                     # новая страница: переносы с прошлой не продолжаются
        for b in bands:
            if b["top"] >= ftop:
                break
            ws = b["ws"]
            name = [w for w in ws if w["x0"] < vmin]
            vals = [w for w in ws if vmin <= w["x0"] < umin]
            units = [w for w in ws if umin <= w["x0"] < rmin]
            refs = [w for w in ws if w["x0"] >= rmin]
            if vals and all(_italic(w) for w in vals) and not name_text_is_method(name):
                vals, units, refs = vals + units + refs, [], []
            name_text = _join(name)
            gap = b["top"] - last_name_top
            # заголовки исследований: жирный текст от левого края без значения
            is_header = name and _bold(name[0]) and (name[0]["x0"] < 66 or (not vals and not units and not refs and
                                                                            not (gap <= 13 and last_kind == "result")))
            if is_header:
                text = _join(ws).replace("Індикатор зони підвищеної уваги", "").strip()
                if last_kind == "header" and gap <= 13 and study:
                    if text not in study["title_raw"]:
                        study["title_raw"] += " " + text
                else:
                    new_study(text)
                result, state, last_kind, last_name_top = None, None, "header", b["top"]
                continue
            if name_text.startswith("Методи"):
                if study is not None:
                    study["method_raw"] = _join(vals + units + refs)
                state, last_kind, last_name_top = "method", "method", b["top"]
                continue
            if state == "method" and not name and vals and all(_italic(w) for w in vals):
                study["method_raw"] = (study["method_raw"] + " " + _join(ws)).strip()
                continue
            if name and (vals or not (gap <= 13 and last_kind == "result")):
                if study is None:
                    new_study("")
                result = {"name_raw": name_text, "value_lines": [], "unit_lines": [], "ref_lines": [],
                          "flag_raw": None, "comment_raw": None, "method_changed": False, "codes": [],
                          "value_top": None}
                study["results"].append(result)
                state, last_kind, last_name_top = "result", "result", b["top"]
            elif name and result is not None:      # перенос длинного названия
                result["name_raw"] += " " + name_text
                last_name_top = b["top"]
            if result is None:
                continue
            if vals:
                result["value_lines"].append(_join(vals))
                if result["value_top"] is None:
                    result["value_top"] = min(w["top"] for w in vals)
            if units:
                result["unit_lines"].append(_join(units))
            if refs:
                result["ref_lines"].append(_join(refs))
        for st in rec["studies"]:
            for r in st["results"]:
                if r.get("value_top") is not None and r.get("_page") is None:
                    r["_page"] = page.page_number
                    if any(t - 2 <= r["value_top"] <= bt + 2 for t, bt in frames):
                        r["flag_raw"] = "рамка"
    # финальная сборка
    for st in rec["studies"]:
        clean = []
        for r in st["results"]:
            value = " ".join(r.pop("value_lines")).strip()
            unit = " ".join(r.pop("unit_lines")).strip()
            ref = "\n".join(r.pop("ref_lines")).strip()
            r.pop("value_top", None), r.pop("_page", None)
            if "додатковому pdf" in value:
                continue                         # «Звіт з описом… знаходиться в додатковому pdf файлі»
            mn = re.match(r"^(.*?)\s*\(в нормі\s*-\s*(.+)\)$", value)
            if mn and not ref:
                value, ref = mn.group(1).strip(), mn.group(2).strip()
            r.update(value_raw=value, unit_raw=unit or None, ref_raw=ref or None,
                     ref_selected=select_ref(ref, sex, age))
            clean.append(r)
        st["results"] = clean
    rec["studies"] = [s for s in rec["studies"] if s["results"]]
    rec["title"] = ", ".join(_short_title(s["title_raw"]) for s in rec["studies"])[:200]
    return rec if rec["studies"] else None


def _short_title(t):
    """«Пакет №4.1 (Печінкові проби + звіт …)» -> «Печінкові проби»; длинные пояснения в скобках убираются."""
    m = re.match(r"^Пакет\s*№\s*[\d.]+\s*\((.+)\)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"\s*\+\s*звіт з описом лабораторних показників", "", t)
    t = re.sub(r"\s*\([^)]{25,}\)", "", t)
    return t.strip()


def parse_synevo_report(pdf, person):
    text = "\n".join(_page_text(p) for p in pdf.pages)
    m = re.search(r"Штрих-код\s+(\d{10})", text)
    d = re.search(r"Дата замовлення\s+(\d{2}\.\d{2}\.\d{4})", text)
    return {"kind": "report", "facility": "Сінево (Synevo)", "order_no": m.group(1) if m else None,
            "collected_at": _iso(DATE_DOT.search(d.group(1))) if d else None,
            "title": "Звіт з описом лабораторних показників"}


# ---------------------------------------------------------------- CSD LAB
DATETIME_LINE = re.compile(r"^\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}$")


def parse_csd(pdf, person):
    first = _page_text(pdf.pages[0])
    rec = {"kind": "lab", "facility": "CSD LAB", "studies": [], "findings": [], "patient": {}}
    m = re.search(r"Замовлення:\s*(CS\d+)", first) or re.search(r"\b(CS\d{5,})\b", first)
    rec["order_no"] = m.group(1) if m else None
    m = re.search(r"Дата забору:\s*(\d{2}-\d{2}-\d{4})", first)
    rec["collected_at"] = _iso(DATE_DASH.search(m.group(1))) if m else None
    m = re.search(r"Дата виконання:\s*(\d{2}-\d{2}-\d{4})", first)
    rec["reported_at"] = _iso(DATE_DASH.search(m.group(1))) if m else None
    m = re.search(r"Дата народження:\s*(\d{2}-\d{2}-\d{4})", first)
    rec["patient"]["birth_date"] = _iso(DATE_DASH.search(m.group(1))) if m else None
    m = re.search(r"Стать:\s*(Чоловіча|Жіноча)", first)
    rec["patient"]["sex"] = {"Чоловіча": "m", "Жіноча": "f"}.get(m.group(1)) if m else None
    m = re.search(r"Лікар:\s*([^\n(]+)", first)
    rec["doctor"] = m.group(1).strip() if m else None
    sex = rec["patient"]["sex"] or (person or {}).get("sex")
    age = _age_years(rec["patient"]["birth_date"] or (person or {}).get("birth_date"), rec["collected_at"])

    study = None
    for page in pdf.pages:
        words = _page_words(page)
        cols = _columns(words)
        if not cols:
            continue
        htop, vmin, umin, rmin = cols
        bands = _bands([w for w in words if w["top"] > htop + 6])
        ftop = _footer_top(bands, ("*Результати досліджень", "Медична лабораторія"))
        block, last = [], None

        def close_block(block):
            """Блок между разделителями: одна строка с названием и значением + норма вокруг неё."""
            nonlocal last
            anchors = [b for b in block if any(w["x0"] < vmin for w in b["ws"]) and
                       any(vmin <= w["x0"] < umin for w in b["ws"])]
            if not anchors or study is None:
                return
            for i, a in enumerate(anchors):
                lo = -1 if i == 0 else a["top"] - 1
                hi = anchors[i + 1]["top"] - 1 if i + 1 < len(anchors) else 10 ** 6
                part = [b for b in block if lo <= b["top"] < hi or (i == 0 and b["top"] < a["top"])]
                r = {"name_raw": _join([w for w in a["ws"] if w["x0"] < vmin]),
                     "value_raw": _join([w for w in a["ws"] if vmin <= w["x0"] < umin]),
                     "unit_raw": " ".join(_join([w for w in b["ws"] if umin <= w["x0"] < rmin]) for b in part).strip() or None,
                     "ref_raw": "\n".join(t for t in (_join([w for w in b["ws"] if w["x0"] >= rmin]) for b in part) if t) or None,
                     "flag_raw": None, "comment_raw": None, "method_changed": False, "codes": []}
                r["ref_selected"] = select_ref(r["ref_raw"], sex, age)
                study["results"].append(r)
                last = r

        for b in bands:
            if b["top"] >= ftop:
                break
            ws = b["ws"]
            text = _join(ws)
            if ws[0]["x0"] < vmin and _bold(ws[0]) and _italic(ws[0]):
                close_block(block); block = []
                study = {"title_raw": text, "material": "blood_venous", "method_raw": None, "results": []}
                rec["studies"].append(study)
                continue
            if DATETIME_LINE.match(text):
                close_block(block); block = []
                continue
            if ws[0]["x0"] < vmin and _italic(ws[0]) and not block and last is not None:
                last["comment_raw"] = ((last["comment_raw"] or "") + " " + text).strip()
                continue
            block.append(b)
        close_block(block)
    rec["studies"] = [s for s in rec["studies"] if s["results"]]
    for s in rec["studies"]:
        if s["title_raw"] in ("Послуги для НСЗУ",) and len(s["results"]) == 1:
            s["title_raw"] = s["results"][0]["name_raw"].split("/")[0].strip()
    rec["title"] = ", ".join(r["name_raw"].split("/")[0].strip() for s in rec["studies"] for r in s["results"])[:200]
    return rec if rec["studies"] else None


def parse_csd_histology(pdf, person):
    text = "\n".join(_page_text(p) for p in pdf.pages)

    def grab(pattern, flags=0):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None
    rec = {"kind": "histology", "facility": "CSD LAB", "findings": [], "patient": {},
           "order_no": grab(r"№ дослідження:\s*(\S+)"),
           "collected_at": _iso(DATE_DASH.search(grab(r"Дата забору:\s*(\S+)") or "")),
           "reported_at": _iso(DATE_DASH.search(grab(r"Дата відповіді:\s*(\S+)") or "")),
           "doctor": grab(r"ПІБ:\s*\S+\s+\S+\s+\S+\s+ПІБ:\s*([^\n№]+?)\s+№"),
           "title": "Патогістологічне дослідження"}
    rec["patient"]["birth_date"] = _iso(DATE_DASH.search(grab(r"Дата народження\s*(\S+)") or ""))
    concl = grab(r"Діагноз\s+(.+?)\n(?:Макроскопічний|$)") or grab(r"Діагноз\s+(.+)")
    codes = [{"system": "ICD-O", "code": c} for c in re.findall(r"ICD-O\s*код\s*([\d/]+)", text)]
    results = []
    for label, pat in (("Вид операції", r"Вид операції\s+(.+)"), ("Клінічний діагноз", r"Клінічний діагноз\s+(.+)")):
        v = grab(pat)
        if v:
            results.append({"name_raw": label, "value_raw": v})
    if concl:
        results.append({"name_raw": "Патоморфологічний висновок", "value_raw": concl, "codes": codes})
    macro = grab(r"Кількість блоків\s+\d+\s*\n(.+?)\n[А-ЯІЇЄҐ][а-яіїєґ'’]+\s+[А-ЯІЇЄҐ]\.", re.S)
    macro = re.sub(r"\s*\n\s*", " ", macro) if macro else None
    blocks = grab(r"Кількість блоків\s+(\d+)")
    if macro or blocks:
        results.append({"name_raw": "Макроскопічний опис", "value_raw": (f"Кількість блоків: {blocks}. " if blocks else "") + (macro or "")})
    rec["summary"] = concl
    rec["studies"] = [{"title_raw": "Патогістологічне дослідження", "material": "tissue", "method_raw": None,
                       "results": [{**{"unit_raw": None, "ref_raw": None, "ref_selected": None, "flag_raw": None,
                                       "comment_raw": None, "method_changed": False, "codes": []}, **r} for r in results]}]
    m = re.search(r"розміром\s+([\d,]+)\s*[хx]\s*([\d,]+)\s*[хx]\s*([\d,]+)\s*см", text)
    if m:
        a, b_, c = (round(float(x.replace(",", ".")) * 10, 1) for x in m.groups())
        rec["findings"].append({"organ": grab(r"Клінічний діагноз\s+.*?(шкіри\s+\S+)") or "", "location_raw": "",
                                "kind": "образование", "size_a": a, "size_b": b_, "size_c": c,
                                "features_raw": concl, "score_system": None, "score_value": None})
    return rec if results else None


# ---------------------------------------------------------------- «Діла» (бета, по текстовой структуре)
DILA_ROW = re.compile(rf"^(?P<name>.+?)\s+(?P<val>[<>]?{NUM}\*?)(?:\s+(?P<unit>[^\s\d<>][^\s]*))?(?:\s+(?P<ref>.*))?$")
DILA_VAL = re.compile(rf"^(?P<val>[<>]?{NUM}\*?)\s*(?P<rest>.*)$")


def parse_dila_text(text, person):
    lines = [l.strip() for l in text.split("\n")]
    rec = {"kind": "lab", "facility": "Діла", "studies": [], "findings": [], "patient": {}}
    m = re.search(r"Дата народж\.\s*(\d{2}\.\d{2}\.\d{4})", text)
    rec["patient"]["birth_date"] = _iso(DATE_DOT.search(m.group(1))) if m else None
    m = re.search(r"Стать:\s*(Чоловіча|Жіноча)", text)
    rec["patient"]["sex"] = {"Чоловіча": "m", "Жіноча": "f"}.get(m.group(1)) if m else None
    m = re.search(r"Дата замовлення:\s*(\d{2}\.\d{2}\.\d{4})", text)
    rec["collected_at"] = _iso(DATE_DOT.search(m.group(1))) if m else None
    m = re.search(r"№ зам\.:\s*(\d+)", text)
    rec["order_no"] = m.group(1) if m else None
    sex = rec["patient"]["sex"] or (person or {}).get("sex")
    age = _age_years(rec["patient"]["birth_date"] or (person or {}).get("birth_date"), rec["collected_at"])
    skip = re.compile(r"^(Назва дослідження|вимірювання|Референтні$|значення Коментарi|РЕЗУЛЬТАТИ ДОСЛІДЖЕНЬ|Пацієнт:|"
                      r"Дата народж|Стать:|Коментарі:|символом \*|№ зам\.|Відповідальна особа|Завідувач|Результат Одиниці)")
    study, pending, last, in_table = None, [], None, False

    def add(name, val, unit, ref):
        nonlocal last
        r = {"name_raw": name.strip(), "value_raw": val, "unit_raw": unit or None, "ref_raw": (ref or "").strip() or None,
             "flag_raw": "*" if val.endswith("*") else None, "comment_raw": None, "method_changed": False, "codes": []}
        r["ref_selected"] = select_ref(r["ref_raw"], sex, age)
        study["results"].append(r)
        last = r

    for i, line in enumerate(lines):
        if not line or skip.match(line):
            continue
        if line.startswith("Комплекс") or (line.startswith("Загальний") and "аналіз" in line):
            study = {"title_raw": line, "material": _material(line), "method_raw": None, "results": []}
            rec["studies"].append(study)
            pending, in_table = [], line.startswith("Загальний")
            continue
        if line.startswith("Первинна проба"):
            continue
        if study is None:
            continue
        if line.startswith(("Увага!", "Змінено тест-систему")) and last:
            last["method_changed"] = True
            last["comment_raw"] = ((last["comment_raw"] or "") + " " + line).strip()
            continue
        mv = DILA_VAL.match(line)
        if pending and mv:                       # блок «название / первинна проба / значення»
            rest = mv.group("rest").split(None, 1)
            unit = rest[0] if rest and not re.match(r"^[\d<>]", rest[0]) else None
            ref = (rest[1] if unit and len(rest) > 1 else mv.group("rest") if not unit else "")
            add(" ".join(pending), mv.group("val"), unit, ref)
            pending = []
            continue
        mr = DILA_ROW.match(line)
        if in_table and mr:                      # строка таблицы ОАК: «название значение единица норма»
            unit, ref = mr.group("unit"), mr.group("ref")
            if unit and re.match(rf"^{NUM}$", unit.replace(",", ".")):
                unit, ref = None, f"{unit} {ref or ''}"
            add(" ".join(pending + [mr.group("name")]), mr.group("val"), unit, ref)
            pending = []
            continue
        ahead = [l for l in lines[i + 1:i + 6] if l][:3]
        name_block = False
        for l in ahead:                          # название на 1–3 строках перед «Первинна проба»
            if l.startswith("Первинна проба"):
                name_block = True
                break
            if DILA_VAL.match(l) or DILA_ROW.match(l):
                break
        comment_like = bool(re.search(rf"{NUM}\s*[-–]\s*\d|^[<>≤≥]|[;:]$", line))
        if (name_block or (in_table and ahead and DILA_ROW.match(ahead[0]))) and not comment_like:
            pending.append(line)
            continue
        if last is not None and not pending:
            last["comment_raw"] = ((last["comment_raw"] or "") + "\n" + line).strip()
    rec["studies"] = [s for s in rec["studies"] if s["results"]]
    rec["title"] = ", ".join(s["title_raw"] for s in rec["studies"])[:200]
    return rec if sum(len(s["results"]) for s in rec["studies"]) >= 2 else None


# ---------------------------------------------------------------- вход
def parse_pdf(path, person=None):
    """-> (lab_code, rec) или (None, None)."""
    if pdfplumber is None:
        return None, None
    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return None, None
            text = "\n".join(_page_text(p) for p in pdf.pages[:8])
            if len(text.strip()) < 40:
                return None, None                 # скан без текстового слоя
            lab = detect(text)
            if lab == "synevo":
                return lab, parse_synevo(pdf, person)
            if lab == "synevo_report":
                return lab, parse_synevo_report(pdf, person)
            if lab == "csd":
                return lab, parse_csd(pdf, person)
            if lab == "csd_histology":
                return lab, parse_csd_histology(pdf, person)
            if lab == "dila":
                return lab, parse_dila_text("\n".join(_page_text(p) for p in pdf.pages), person)
    except Exception:  # noqa: BLE001 — локальный разбор не должен ломать импорт
        import traceback
        traceback.print_exc()
    return None, None
