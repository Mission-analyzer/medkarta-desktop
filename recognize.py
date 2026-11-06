"""Распознавание бланка через Anthropic API: Claude читает PDF или фото и возвращает сырые поля.

Сопоставление со справочником делает программа (reference.py), а не модель.
"""
import base64
import json
import re
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"

PROMPT = """Ты переносишь медицинский документ в личный архив пациента. Перед тобой один документ \
(бланк лаборатории, протокол УЗИ/МРТ/КТ, ЭКГ, заключение врача, выписка, гистология). \
Твоя задача — точно переписать его содержимое в JSON. Ничего не интерпретируй и не исправляй: \
переписывай названия, значения, единицы и нормы так, как они напечатаны, на языке бланка.

Ответь ТОЛЬКО JSON такого вида, без пояснений и без ```:
{
 "kind": "lab | imaging | ecg | visit | discharge | histology | other",
 "facility": "лаборатория или клиника",
 "order_no": "номер заказа / исследования / штрих-код или null",
 "collected_at": "YYYY-MM-DD — дата забора материала или проведения исследования",
 "reported_at": "YYYY-MM-DD — дата выдачи результата или null",
 "doctor": "врач, выполнивший исследование, или null",
 "patient": {"sex": "m | f | null", "birth_date": "YYYY-MM-DD или null"},
 "title": "короткое название документа, например «Общий анализ крови, моча, печёночные пробы»",
 "summary": "заключение / вывод текстом, если он есть, иначе null",
 "studies": [
  {
   "title_raw": "название исследования как в бланке",
   "material": "blood_venous | blood_capillary | urine | urine_24h | stool | swab | semen | sputum | tissue | imaging | other",
   "method_raw": "методика или анализатор, если указаны, иначе null",
   "results": [
    {
     "name_raw": "название показателя ПОЛНОСТЬЮ как в бланке, вместе со скобками и префиксами вроде «Мікроскопія осаду сечі:»",
     "value_raw": "значение как напечатано: «7.94», «< 0.15», «5.330*», «не виявлені», «1:160»",
     "unit_raw": "единица как напечатана или null",
     "ref_raw": "норма целиком как напечатана, все строки через \\n, или null",
     "ref_selected": "одна строка нормы, которая относится к ЭТОМУ пациенту по полу и возрасту (например «Чоловіки: 130.0 - 160.0»), или null",
     "flag_raw": "отметка лаборатории: «*», «H», «L», «рамка» (если значение обведено), иначе null",
     "comment_raw": "комментарий к строке, иначе null",
     "method_changed": false,
     "codes": [{"system": "ICD-O | ICD-10", "code": "..."}]
    }
   ]
  }
 ],
 "findings": [
  {"organ": "орган", "location_raw": "расположение как в тексте", "kind": "узел | киста | кальцинат | полип | лимфоузел | образование | другое",
   "size_a": 0, "size_b": 0, "size_c": 0, "features_raw": "описание как в тексте",
   "score_system": "TI-RADS | BI-RADS | O-RADS | Bosniak | null", "score_value": "категория или null"}
 ]
}

Правила:
- Каждая строка бланка с результатом — отдельный элемент results. Если показатель дан и в %, и в абсолютных числах, это две строки.
- Числа переписывай с точкой как разделителем, знаки «<», «>», «≤» и «*» сохраняй в value_raw.
- method_changed = true, только если лаборатория прямо пишет о смене методики или тест-системы для этой строки.
- Для протоколов УЗИ/МРТ/КТ: размеры органов — results (material = imaging, name_raw как в тексте, например «Правая доля: объём»); \
образования — findings, размеры в миллиметрах (если в см — переведи в мм); описание органов без чисел — results с текстовым value_raw.
- Для гистологии: части бланка (клинический диагноз, макроскопическое описание, вывод) — results с текстовым value_raw, коды — в codes.
- Служебный текст (адреса, аккредитация, подписи, рекламу, пояснения о том, что такое показатель) не переписывай.
- Если чего-то нет — null или пустой список. Ничего не выдумывай."""


class RecognizeError(Exception):
    """code — ключ сообщения (переводится в app.py), detail — подробности для подстановки."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


def recognize(path, mime, api_key, model=None, timeout=240):
    data = base64.b64encode(open(path, "rb").read()).decode()
    if mime == "application/pdf":
        part = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": data}}
    elif mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        part = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    else:
        raise RecognizeError("rec_bad_type")
    body = {"model": model or DEFAULT_MODEL, "max_tokens": 16000,
            "messages": [{"role": "user", "content": [part, {"type": "text", "text": PROMPT}]}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        if e.code == 401:
            raise RecognizeError("rec_bad_key")
        raise RecognizeError("rec_api_error", f"{e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RecognizeError("rec_no_connection", str(e.reason))
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    if out.get("stop_reason") == "max_tokens":
        raise RecognizeError("rec_too_long")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise RecognizeError("rec_bad_json")
