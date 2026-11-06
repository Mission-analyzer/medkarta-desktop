"""Сообщения, которые формирует Python-часть. Тексты интерфейса — в ui/i18n.js."""
import locale

LANGS = ("uk", "ru", "en")

MSG = {
    "name_required": {"ru": "Укажите имя.", "uk": "Вкажіть ім'я.", "en": "Enter a name."},
    "person_has_docs": {"ru": "У человека есть документы — сначала удалите их.",
                        "uk": "У людини є документи — спершу видаліть їх.",
                        "en": "This person has documents. Delete them first."},
    "doc_not_found": {"ru": "Документ не найден.", "uk": "Документ не знайдено.", "en": "Document not found."},
    "file_type": {"ru": "«{name}»: поддерживаются PDF, JPG, PNG и WebP.",
                  "uk": "«{name}»: підтримуються PDF, JPG, PNG і WebP.",
                  "en": "“{name}”: only PDF, JPG, PNG and WebP are supported."},
    "no_person": {"ru": "Не выбран человек.", "uk": "Не вибрано людину.", "en": "No person selected."},
    "portable_dir": {"ru": "В переносном режиме архив всегда лежит в папке data рядом с программой.",
                     "uk": "У портативному режимі архів завжди лежить у теці data поруч із програмою.",
                     "en": "In portable mode the archive always lives in the data folder next to the program."},
    "dir_no_archive": {"ru": "В выбранной папке нет архива, и она не пустая. Выберите папку с архивом или пустую папку — в ней будет создан новый архив.",
                       "uk": "У вибраній теці немає архіву, і вона не порожня. Виберіть теку з архівом або порожню теку — у ній буде створено новий архів.",
                       "en": "That folder has no archive and is not empty. Choose a folder with an archive or an empty folder — a new archive will be created there."},
    "internal": {"ru": "Внутренняя ошибка: {detail}", "uk": "Внутрішня помилка: {detail}", "en": "Internal error: {detail}"},
    "rec_bad_type": {"ru": "Распознать можно только PDF, JPG, PNG или WebP.", "uk": "Розпізнати можна лише PDF, JPG, PNG або WebP.",
                     "en": "Only PDF, JPG, PNG or WebP can be recognised."},
    "rec_bad_key": {"ru": "Ключ API не принят. Проверьте его в настройках.", "uk": "Ключ API не прийнято. Перевірте його в налаштуваннях.",
                    "en": "The API key was rejected. Check it in Settings."},
    "rec_api_error": {"ru": "Ошибка API {detail}", "uk": "Помилка API {detail}", "en": "API error {detail}"},
    "rec_no_connection": {"ru": "Нет связи с сервером Anthropic: {detail}", "uk": "Немає зв'язку із сервером Anthropic: {detail}",
                          "en": "Cannot reach the Anthropic server: {detail}"},
    "rec_too_long": {"ru": "Документ слишком длинный для одного распознавания: ответ оборвался.",
                     "uk": "Документ задовгий для одного розпізнавання: відповідь обірвалася.",
                     "en": "The document is too long to recognise in one go: the response was cut off."},
    "rec_bad_json": {"ru": "Не удалось разобрать ответ распознавания. Попробуйте ещё раз.",
                     "uk": "Не вдалося розібрати відповідь розпізнавання. Спробуйте ще раз.",
                     "en": "Could not read the recognition response. Please try again."},
    "obs_empty": {"ru": "Введите хотя бы одно значение.", "uk": "Введіть хоча б одне значення.", "en": "Enter at least one value."},
    "obs_no_symptom": {"ru": "Выберите или впишите симптом.", "uk": "Виберіть або впишіть симптом.", "en": "Choose or type a symptom."},
    "warn_birth": {"ru": "Дата рождения в бланке не совпадает с карточкой выбранного человека.",
                   "uk": "Дата народження в бланку не збігається з карткою вибраної людини.",
                   "en": "The date of birth on the form does not match the selected person."},
    "warn_sex": {"ru": "Пол в бланке не совпадает с карточкой выбранного человека.",
                 "uk": "Стать у бланку не збігається з карткою вибраної людини.",
                 "en": "The sex on the form does not match the selected person."},
}


def default_lang():
    """Язык Windows: украинский -> uk, русский -> ru, остальное -> en."""
    try:
        import ctypes
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary = lcid & 0x3FF
        return {0x22: "uk", 0x19: "ru"}.get(primary, "en")
    except Exception:  # noqa: BLE001  (не Windows)
        loc = (locale.getlocale()[0] or "").lower()
        return "uk" if loc.startswith(("uk", "ukrainian")) else "ru" if loc.startswith(("ru", "russian")) else "en"


def msg(key, lang, **kw):
    m = MSG.get(key)
    if not m:
        return key
    return (m.get(lang) or m["en"]).format(**kw)
