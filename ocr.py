"""Локальный OCR через Tesseract: сканы PDF и фото -> текст.

Tesseract ищется в таком порядке: папка tesseract рядом с программой (переносная сборка),
стандартная установка Windows, PATH. Если его нет, программа работает без OCR.
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageFilter, ImageOps
except ImportError:  # без Pillow/numpy OCR недоступен
    np = Image = None

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WANT_LANGS = ("ukr", "rus", "eng")
MAX_PAGES = 8


def find_tesseract():
    cands = [APP_DIR / "tesseract" / "tesseract.exe", APP_DIR / "tesseract" / "tesseract"]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [Path(base) / "Tesseract-OCR" / "tesseract.exe", Path(base) / "Programs" / "Tesseract-OCR" / "tesseract.exe"]
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which("tesseract")


def _env(exe):
    env = dict(os.environ)
    td = Path(exe).parent / "tessdata"
    if td.is_dir():
        env["TESSDATA_PREFIX"] = str(td)
    return env


_status_cache = None


def status():
    """{'available': bool, 'path': str|None, 'langs': [...]}"""
    global _status_cache
    if _status_cache is not None:
        return _status_cache
    exe = find_tesseract()
    st = {"available": False, "path": exe, "langs": []}
    if exe and Image is not None:
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=20,
                                 env=_env(exe), creationflags=NO_WINDOW)
            langs = [l.strip() for l in out.stdout.splitlines()[1:] if l.strip()]
            st["langs"] = [l for l in WANT_LANGS if l in langs]
            st["available"] = bool(st["langs"])
        except (OSError, subprocess.SubprocessError):
            pass
    _status_cache = st
    return st


def preprocess(img):
    """Серый фон -> белый: делим на размытую копию (оценка фона). Поворот по EXIF для фото с телефона."""
    img = ImageOps.exif_transpose(img).convert("L")
    if max(img.size) > 4200:
        k = 4200 / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)))
    small = img.resize((max(1, img.width // 4), max(1, img.height // 4))).filter(ImageFilter.GaussianBlur(12))
    bg = np.asarray(small.resize(img.size), dtype=np.float32) + 1.0
    norm = np.clip(np.asarray(img, dtype=np.float32) / bg * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(norm)


def page_images(path, mime, dpi=300, max_pages=MAX_PAGES):
    """PDF или фото -> список изображений страниц (PIL)."""
    if mime == "application/pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        return [pdf[i].render(scale=dpi / 72).to_pil() for i in range(min(len(pdf), max_pages))]
    return [Image.open(path)]


def ocr_file(path, mime, progress=None):
    """-> текст всех страниц (страницы разделены строкой «\\f») или None, если OCR недоступен."""
    st = status()
    if not st["available"]:
        return None
    exe, langs = st["path"], "+".join(st["langs"])
    texts = []
    pages = page_images(path, mime)
    for i, img in enumerate(pages):
        if progress:
            progress(i + 1, len(pages))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "page.png"
            preprocess(img).save(p)
            out = subprocess.run([exe, str(p), "stdout", "-l", langs, "--psm", "4"], capture_output=True,
                                 timeout=300, env=_env(exe), creationflags=NO_WINDOW)
            texts.append(out.stdout.decode("utf-8", "replace"))
    return "\f".join(texts)


def preview_data_urls(path, mime, width=1400, max_pages=MAX_PAGES):
    """Страницы для просмотра рядом с формой ввода: JPEG data URL."""
    out = []
    for img in page_images(path, mime, dpi=150, max_pages=max_pages):
        img = ImageOps.exif_transpose(img).convert("RGB")
        if img.width > width:
            img = img.resize((width, int(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        out.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())
    return out


def is_mostly_unreadable(text):
    """Грубая оценка: если почти нет слов из 4+ букв, это рукопись или очень плохой скан."""
    import re
    words = re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]{4,}", text or "")
    return len(words) < 15
