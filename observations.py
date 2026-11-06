"""Раздел «Наблюдения»: домашние измерения, эпизоды (симптомы) и факторы дня, поиск связей между ними.

Идея связей взята у дневников симптомов: сравниваем, как часто симптом бывает после дней с фактором
и после дней без него. Ночной эпизод относится к предыдущему дню (сутки считаются с 06:00).
"""
import json
from datetime import datetime, timedelta

from db import new_id, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs_measurements (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL, taken_at TEXT NOT NULL, type TEXT NOT NULL,
  v1 REAL, v2 REAL, unit TEXT, context TEXT, arm TEXT, position TEXT, note TEXT, group_id TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS obs_meas_person ON obs_measurements(person_id, taken_at);
CREATE TABLE IF NOT EXISTS obs_episodes (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL, started_at TEXT NOT NULL, symptom TEXT NOT NULL,
  duration_min REAL, severity INTEGER, pulse REAL, sys REAL, dia REAL, note TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS obs_ep_person ON obs_episodes(person_id, started_at);
CREATE TABLE IF NOT EXISTS obs_factors (
  person_id TEXT NOT NULL, day TEXT NOT NULL, factor TEXT NOT NULL, value TEXT,
  PRIMARY KEY (person_id, day, factor));
"""

# Измерения: тип -> единица, норма (для домашних измерений), подписи
MEASURES = {
    "bp": {"unit": "мм рт. ст.", "two": True},
    "pulse": {"unit": "уд/мин"},
    "weight": {"unit": "кг"},
    "temp": {"unit": "°C"},
    "spo2": {"unit": "%"},
    "glucose": {"unit": "ммоль/л"},
}
# Нормы для графиков. Давление — ESH 2023 для домашних измерений; пульс — покой.
SERIES = {
    "bp_sys": {"type": "bp", "field": "v1", "high": 135, "source": "ESH 2023: дома < 135/85"},
    "bp_dia": {"type": "bp", "field": "v2", "high": 85, "source": "ESH 2023: дома < 135/85"},
    "pulse": {"type": "pulse", "field": "v1", "low": 50, "high": 100, "source": "пульс в покое"},
    "weight": {"type": "weight", "field": "v1"},
    "temp": {"type": "temp", "field": "v1", "low": 35.9, "high": 37.0, "source": "общепринятое"},
    "spo2": {"type": "spo2", "field": "v1", "low": 95, "source": "общепринятое"},
    "glucose": {"type": "glucose", "field": "v1", "low": 3.9, "high": 5.5, "source": "натощак, глюкометр"},
}
SERIES_NAMES = {
    "uk": {"bp_sys": "Тиск систолічний (вдома)", "bp_dia": "Тиск діастолічний (вдома)", "pulse": "Пульс (вдома)",
           "weight": "Вага", "temp": "Температура", "spo2": "Сатурація", "glucose": "Цукор (глюкометр)"},
    "ru": {"bp_sys": "Давление систолическое (дома)", "bp_dia": "Давление диастолическое (дома)", "pulse": "Пульс (дома)",
           "weight": "Вес", "temp": "Температура", "spo2": "Сатурация", "glucose": "Сахар (глюкометр)"},
    "en": {"bp_sys": "Systolic BP (home)", "bp_dia": "Diastolic BP (home)", "pulse": "Pulse (home)",
           "weight": "Weight", "temp": "Temperature", "spo2": "Oxygen saturation", "glucose": "Glucose (meter)"},
}

DEFAULT_CONFIG = {
    "uk": {
        "symptoms": ["Серцебиття", "Перебої в серці", "Запаморочення", "Головний біль", "Біль у грудях", "Задишка", "Нічне пробудження"],
        "factors": [
            {"key": "load", "label": "Фізичне навантаження", "type": "choice", "options": ["немає", "легке", "середнє", "важке"]},
            {"key": "tea_evening", "label": "Чай/кава ввечері", "type": "choice", "options": ["немає", "до 0,5 л", "понад 0,5 л"]},
            {"key": "last_cup", "label": "Остання чашка чаю/кави", "type": "choice", "options": ["до 14:00", "14–18", "після 18:00"]},
            {"key": "alcohol", "label": "Алкоголь", "type": "bool"},
            {"key": "late_dinner", "label": "Пізня вечеря", "type": "bool"},
            {"key": "little_water", "label": "Мало води вдень", "type": "bool"},
        ]},
    "ru": {
        "symptoms": ["Сердцебиение", "Перебои в сердце", "Головокружение", "Головная боль", "Боль в груди", "Одышка", "Ночное пробуждение"],
        "factors": [
            {"key": "load", "label": "Физическая нагрузка", "type": "choice", "options": ["нет", "лёгкая", "средняя", "тяжёлая"]},
            {"key": "tea_evening", "label": "Чай/кофе вечером", "type": "choice", "options": ["нет", "до 0,5 л", "больше 0,5 л"]},
            {"key": "last_cup", "label": "Последняя чашка чая/кофе", "type": "choice", "options": ["до 14:00", "14–18", "после 18:00"]},
            {"key": "alcohol", "label": "Алкоголь", "type": "bool"},
            {"key": "late_dinner", "label": "Поздний ужин", "type": "bool"},
            {"key": "little_water", "label": "Мало воды днём", "type": "bool"},
        ]},
    "en": {
        "symptoms": ["Palpitations", "Skipped beats", "Dizziness", "Headache", "Chest pain", "Shortness of breath", "Night waking"],
        "factors": [
            {"key": "load", "label": "Physical exertion", "type": "choice", "options": ["none", "light", "moderate", "heavy"]},
            {"key": "tea_evening", "label": "Tea/coffee in the evening", "type": "choice", "options": ["none", "up to 0.5 l", "over 0.5 l"]},
            {"key": "last_cup", "label": "Last cup of tea/coffee", "type": "choice", "options": ["before 14:00", "14–18", "after 18:00"]},
            {"key": "alcohol", "label": "Alcohol", "type": "bool"},
            {"key": "late_dinner", "label": "Late dinner", "type": "bool"},
            {"key": "little_water", "label": "Little water during the day", "type": "bool"},
        ]},
}

DAY_START_HOUR = 6   # эпизод в 01:00 относится к предыдущему дню


def episode_day(started_at):
    dt = datetime.fromisoformat(started_at[:16])
    return (dt - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def _f(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None


class Observations:
    def __init__(self, db):
        self.db = db
        with db.lock:
            db.con.executescript(SCHEMA)
            db.con.commit()

    # ---------- настройки списков ----------
    def config(self, lang):
        raw = self.db.get_meta("obs_config")
        if raw:
            try:
                return json.loads(raw)
            except ValueError:
                pass
        return json.loads(json.dumps(DEFAULT_CONFIG.get(lang, DEFAULT_CONFIG["en"])))

    def save_config(self, cfg):
        syms = [s.strip() for s in cfg.get("symptoms", []) if str(s).strip()]
        facs = []
        for f in cfg.get("factors", []):
            label = str(f.get("label", "")).strip()
            if not label:
                continue
            key = f.get("key") or ("f_" + new_id("x")[2:10])
            typ = f.get("type") if f.get("type") in ("bool", "choice") else "bool"
            opts = [o.strip() for o in f.get("options", []) if str(o).strip()] if typ == "choice" else []
            facs.append({"key": key, "label": label, "type": typ, "options": opts})
        self.db.set_meta("obs_config", json.dumps({"symptoms": syms, "factors": facs}, ensure_ascii=False))
        return {"symptoms": syms, "factors": facs}

    # ---------- запись ----------
    def save_measurement(self, pid, m):
        """Одна запись формы -> несколько строк (давление, пульс, вес...) с общим group_id."""
        taken = (m.get("taken_at") or now())[:16]
        gid = m.get("group_id") or new_id("g")
        ctx = {k: m.get(k) for k in ("context", "arm", "position", "note")}
        rows = []
        if _f(m.get("sys")) is not None or _f(m.get("dia")) is not None:
            rows.append(("bp", _f(m.get("sys")), _f(m.get("dia"))))
        for t in ("pulse", "weight", "temp", "spo2", "glucose"):
            if _f(m.get(t)) is not None:
                rows.append((t, _f(m.get(t)), None))
        if not rows:
            raise ValueError("obs_empty")
        with self.db.lock:
            self.db.con.execute("DELETE FROM obs_measurements WHERE group_id=?", (gid,))
            for typ, v1, v2 in rows:
                self.db.con.execute(
                    "INSERT INTO obs_measurements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("m"), pid, taken, typ, v1, v2, MEASURES[typ]["unit"], ctx["context"], ctx["arm"],
                     ctx["position"], ctx["note"], gid, now()))
            self.db.con.commit()
        return gid

    def save_episode(self, pid, e):
        if not str(e.get("symptom", "")).strip():
            raise ValueError("obs_no_symptom")
        eid = e.get("id") or new_id("e")
        with self.db.lock:
            self.db.con.execute("INSERT OR REPLACE INTO obs_episodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (eid, pid, (e.get("started_at") or now())[:16], e["symptom"].strip(),
                                 _f(e.get("duration_min")), int(e["severity"]) if e.get("severity") else None,
                                 _f(e.get("pulse")), _f(e.get("sys")), _f(e.get("dia")), e.get("note"), now()))
            self.db.con.commit()
        return eid

    def save_factors(self, pid, day, values):
        with self.db.lock:
            self.db.con.execute("DELETE FROM obs_factors WHERE person_id=? AND day=?", (pid, day))
            for k, v in (values or {}).items():
                if v in (None, "", False):
                    continue
                self.db.con.execute("INSERT INTO obs_factors VALUES (?,?,?,?)", (pid, day, k, "1" if v is True else str(v)))
            self.db.con.commit()
        return True

    def get_factors(self, pid, day):
        return {r["factor"]: r["value"] for r in self.db.q("SELECT factor, value FROM obs_factors WHERE person_id=? AND day=?", (pid, day))}

    def delete(self, kind, ident, pid):
        with self.db.lock:
            if kind == "measurement":
                self.db.con.execute("DELETE FROM obs_measurements WHERE group_id=? AND person_id=?", (ident, pid))
            elif kind == "episode":
                self.db.con.execute("DELETE FROM obs_episodes WHERE id=? AND person_id=?", (ident, pid))
            elif kind == "factors":
                self.db.con.execute("DELETE FROM obs_factors WHERE day=? AND person_id=?", (ident, pid))
            self.db.con.commit()
        return True

    # ---------- чтение ----------
    def timeline(self, pid, days=60):
        since = (datetime.now() - timedelta(days=days)).date().isoformat()
        meas = {}
        for r in self.db.q("SELECT * FROM obs_measurements WHERE person_id=? AND taken_at>=? ORDER BY taken_at", (pid, since)):
            g = meas.setdefault(r["group_id"], {"kind": "measurement", "id": r["group_id"], "at": r["taken_at"],
                                                "context": r["context"], "arm": r["arm"], "position": r["position"],
                                                "note": r["note"], "values": {}})
            if r["type"] == "bp":
                g["values"]["sys"], g["values"]["dia"] = r["v1"], r["v2"]
            else:
                g["values"][r["type"]] = r["v1"]
        eps = [{"kind": "episode", **r, "at": r["started_at"], "day": episode_day(r["started_at"])}
               for r in self.db.q("SELECT * FROM obs_episodes WHERE person_id=? AND started_at>=?", (pid, since))]
        facs = {}
        for r in self.db.q("SELECT * FROM obs_factors WHERE person_id=? AND day>=?", (pid, since)):
            facs.setdefault(r["day"], {})[r["factor"]] = r["value"]
        items = list(meas.values()) + eps + [{"kind": "factors", "id": d, "at": d + "T23:59", "values": v} for d, v in facs.items()]
        return sorted(items, key=lambda x: x["at"], reverse=True)

    def series(self, pid, lang):
        """Серии для «Показателей»: давление, пульс и т.д. с нормами."""
        names = SERIES_NAMES.get(lang, SERIES_NAMES["en"])
        out = []
        for sid, s in SERIES.items():
            rows = self.db.q(f"SELECT taken_at, {s['field']} v FROM obs_measurements WHERE person_id=? AND type=? "
                             f"AND {s['field']} IS NOT NULL ORDER BY taken_at", (pid, s["type"]))
            if not rows:
                continue
            pts = []
            for r in rows:
                v, lo, hi = r["v"], s.get("low"), s.get("high")
                flag = "high" if hi is not None and v > hi else "low" if lo is not None and v < lo else ("normal" if (lo or hi) else None)
                pts.append({"date": r["taken_at"], "value": v, "op": None, "flag": flag, "low": lo, "high": hi,
                            "method_changed": 0, "doc_id": None, "facility": ""})
            out.append({"id": "obs:" + sid, "name": names[sid], "unit": MEASURES[s["type"]]["unit"], "matched": True, "points": pts})
        return out

    # ---------- связи ----------
    def correlations(self, pid, lang, min_days=5):
        """Для каждого симптома и фактора: доля дней с эпизодом при каждом значении фактора."""
        cfg = self.config(lang)
        fac_days = {}
        for r in self.db.q("SELECT day, factor, value FROM obs_factors WHERE person_id=?", (pid,)):
            fac_days.setdefault(r["day"], {})[r["factor"]] = r["value"]
        days = sorted(fac_days)
        if len(days) < min_days:
            return {"days": len(days), "min_days": min_days, "items": []}
        ep_days = {}
        for r in self.db.q("SELECT started_at, symptom FROM obs_episodes WHERE person_id=?", (pid,)):
            ep_days.setdefault(r["symptom"], set()).add(episode_day(r["started_at"]))
        items = []
        for sym, sdays in ep_days.items():
            for f in cfg["factors"]:
                groups = {}
                for d in days:
                    val = fac_days[d].get(f["key"])
                    if f["type"] == "bool":
                        val = "yes" if val else "no"
                    elif not val:
                        continue
                    g = groups.setdefault(val, [0, 0])
                    g[1] += 1
                    if d in sdays:
                        g[0] += 1
                if len(groups) < 2:
                    continue
                rates = {k: v[0] / v[1] for k, v in groups.items() if v[1]}
                spread = max(rates.values()) - min(rates.values())
                order = (["yes", "no"] if f["type"] == "bool" else [o for o in f.get("options", []) if o in groups])
                items.append({"symptom": sym, "factor": f["key"], "factor_label": f["label"], "type": f["type"],
                              "groups": [{"value": k, "with": groups[k][0], "days": groups[k][1]} for k in order if k in groups],
                              "spread": round(spread, 2)})
        items.sort(key=lambda x: -x["spread"])
        return {"days": len(days), "min_days": min_days, "items": items}
