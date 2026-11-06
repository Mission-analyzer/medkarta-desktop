"""«Копия для телефона»: один HTML-файл со всем архивом для просмотра без интернета.

Внутри: документы с показателями и отметками, заключения, находки, графики показателей,
оригиналы (страницы PDF и фото переведены в JPEG — телефонный браузер показывает их надёжнее, чем PDF).
"""
import base64
import io
import json
from datetime import date
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None

PAGE_WIDTH = 1100       # ширина страницы оригинала в пикселях: читается при увеличении и не раздувает файл
JPEG_QUALITY = 68
MAX_PAGES_PER_FILE = 12

STRINGS = {
    "uk": {"docs": "Документи", "charts": "Графіки", "doctor": "Лікарю", "search": "Пошук", "out": "поза нормою",
           "in_norm": "ще {n} у нормі", "norm": "норма", "originals": "Оригінал", "pages": "стор.", "summary": "Висновок",
           "notes": "Нотатки", "findings": "Знахідки", "back": "Назад", "no_docs": "Документів немає",
           "out_h": "Поза нормою (останні значення)", "concl_h": "Висновки досліджень", "home_h": "Домашні вимірювання",
           "prev": "раніше", "values": "Усі значення", "years": "р.", "made": "Копію створено", "nothing_out": "Відхилень немає",
           "text_results": "Опис", "copy_note": "Копія для перегляду. Основний архів — у програмі «Медкарта»."},
    "ru": {"docs": "Документы", "charts": "Графики", "doctor": "Врачу", "search": "Поиск", "out": "вне нормы",
           "in_norm": "ещё {n} в норме", "norm": "норма", "originals": "Оригинал", "pages": "стр.", "summary": "Заключение",
           "notes": "Заметки", "findings": "Находки", "back": "Назад", "no_docs": "Документов нет",
           "out_h": "Вне нормы (последние значения)", "concl_h": "Заключения исследований", "home_h": "Домашние измерения",
           "prev": "раньше", "values": "Все значения", "years": "г.", "made": "Копия создана", "nothing_out": "Отклонений нет",
           "text_results": "Описание", "copy_note": "Копия для просмотра. Основной архив — в программе «Медкарта»."},
    "en": {"docs": "Documents", "charts": "Charts", "doctor": "Doctor", "search": "Search", "out": "out of range",
           "in_norm": "{n} more in range", "norm": "range", "originals": "Original", "pages": "p.", "summary": "Conclusion",
           "notes": "Notes", "findings": "Findings", "back": "Back", "no_docs": "No documents",
           "out_h": "Out of range (latest values)", "concl_h": "Imaging and visit conclusions", "home_h": "Home measurements",
           "prev": "earlier", "values": "All values", "years": "y", "made": "Copy made", "nothing_out": "Nothing out of range",
           "text_results": "Description", "copy_note": "View-only copy. The main archive is in the Medkarta program."},
}
KIND = {
    "uk": {"lab": "Аналізи", "imaging": "УЗД / МРТ / КТ", "ecg": "ЕКГ", "visit": "Прийом лікаря", "discharge": "Виписка", "histology": "Гістологія", "other": "Інше"},
    "ru": {"lab": "Анализы", "imaging": "УЗИ / МРТ / КТ", "ecg": "ЭКГ", "visit": "Приём врача", "discharge": "Выписка", "histology": "Гистология", "other": "Другое"},
    "en": {"lab": "Lab tests", "imaging": "Imaging", "ecg": "ECG", "visit": "Doctor visit", "discharge": "Discharge", "histology": "Histology", "other": "Other"},
}


def _jpeg(img):
    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.width > PAGE_WIDTH:
        img = img.resize((PAGE_WIDTH, int(img.height * PAGE_WIDTH / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def file_pages(path, mime):
    if Image is None or not Path(path).is_file():
        return []
    try:
        if mime == "application/pdf":
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(path))
            return [_jpeg(pdf[i].render(scale=130 / 72).to_pil()) for i in range(min(len(pdf), MAX_PAGES_PER_FILE))]
        if mime and mime.startswith("image/"):
            return [_jpeg(Image.open(path))]
    except Exception:  # noqa: BLE001 — битый файл не должен ломать всю копию
        return []
    return []


def build(api, person_id):
    """Собрать данные и HTML. api — объект app.Api (берём из него базу, справочник и наблюдения)."""
    lang = api._settings.get("lang", "ru")
    person = next((p for p in api._db.persons() if p["id"] == person_id), {}) or {}
    age = None
    try:
        b = date.fromisoformat(person.get("birth_date") or "")
        t = date.today()
        age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    except ValueError:
        pass
    names = {k: (a.get("name_" + lang) or a["name_ru"]) for k, a in api._ref.analytes.items()}
    docs = []
    for row in api._db.list_documents(person_id):
        d = api._db.get_document(row["id"])
        studies = []
        for s in d["studies"]:
            res = []
            for r in s["results"]:
                ref = r.get("ref_selected") or (r.get("ref_raw") or "").split("\n")[0]
                if not ref and (r.get("ref_low") is not None or r.get("ref_high") is not None):
                    lo, hi = r.get("ref_low"), r.get("ref_high")
                    ref = f"{'' if lo is None else lo}–{'' if hi is None else hi}"
                numeric = r.get("result_type") in ("numeric", "titer")
                res.append({"n": names.get(r.get("analyte_key")) or r.get("name_raw") or "",
                            "v": (r.get("value_raw") or "") if numeric else (r.get("value_text") or r.get("value_raw") or ""),
                            "u": (r.get("unit_raw") or "") if numeric else "", "f": r.get("flag") or ("lab" if r.get("flag_lab") and not numeric else None),
                            "r": ref or "", "t": "num" if numeric else "text"})
            studies.append({"t": s.get("title_raw") or "", "r": res})
        pages = []
        for f in d["files"]:
            pages += file_pages(api._abs(f["path"]), f.get("mime"))
        docs.append({"id": d["id"], "k": d.get("kind") or "other", "kl": KIND.get(lang, KIND["en"]).get(d.get("kind"), d.get("kind")),
                     "t": d.get("title") or "", "d": d.get("collected_at") or "", "fac": d.get("facility") or "",
                     "doc": d.get("doctor") or "", "sum": d.get("summary") or "", "notes": d.get("notes") or "",
                     "st": studies, "fi": [{"o": ", ".join(x for x in (f.get("organ"), f.get("location_raw")) if x), "k": f.get("kind") or "",
                                            "s": " × ".join(f"{v:g}" for v in (f.get("size_a"), f.get("size_b"), f.get("size_c")) if v),
                                            "c": " ".join(x for x in (f.get("score_system"), f.get("score_value")) if x),
                                            "d": f.get("features_raw") or ""} for f in d["findings"]],
                     "pg": pages, "q": " ".join(x for x in (d.get("title"), d.get("facility"), d.get("doctor"), d.get("summary"),
                                                            d.get("notes"), (d.get("ocr_text") or "")[:4000],
                                                            " ".join(" ".join(str(f.get(k) or "") for k in ("organ", "location_raw", "kind", "features_raw"))
                                                                     for f in d["findings"]),
                                                            " ".join(str(r.get("value_text") or "") for s_ in d["studies"] for r in s_["results"]
                                                                     if r.get("result_type") not in ("numeric", "titer"))) if x)})
    series = []
    for s in api.indicators(person_id):
        series.append({"id": s["id"], "n": s["name"], "u": s.get("unit") or "", "home": s["id"].startswith("obs:"),
                       "p": [{"d": p["date"], "v": p["value"], "op": p.get("op"), "f": p.get("flag"),
                              "lo": p.get("low"), "hi": p.get("high")} for p in s["points"]]})
    data = {"lang": lang, "S": STRINGS.get(lang, STRINGS["en"]), "person": {"name": person.get("name") or "", "age": age},
            "made": date.today().isoformat(), "docs": docs, "series": series}
    tpl = (Path(api._base) / "ui" / "phone.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", payload).replace("__LANG__", lang)
