from __future__ import annotations

import base64
import mimetypes
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_TEXT_CHARS = 60_000
MAX_FILE_MB = 50
MAX_VIDEO_FRAMES = 8
IMG_MAX_SIDE = 1568

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
TEXT_EXT = {".txt", ".md", ".py", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".js", ".ts", ".tsx", ".jsx",
            ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".csv", ".tsv", ".html", ".css", ".xml",
            ".sql", ".sh", ".bat", ".ps1", ".rs", ".go", ".kt", ".swift", ".lua", ".log", ".rst", ".tex"}


@dataclass
class Attachment:
    name: str
    kind: str
    text: str = ""
    images: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def _encode_frame(cv2, frame) -> tuple[str, str]:
    h, w = frame.shape[:2]
    scale = min(1.0, IMG_MAX_SIDE / max(h, w))
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("No se pudo codificar la imagen.")
    return "image/jpeg", base64.b64encode(buf.tobytes()).decode()


def _image(path: Path) -> Attachment:
    cv2 = _cv2()
    if cv2 is not None:
        import numpy as np
        frame = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is not None:
            return Attachment(path.name, "image", images=[_encode_frame(cv2, frame)])
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return Attachment(path.name, "image", images=[(mime, base64.b64encode(path.read_bytes()).decode())],
                      note="sin redimensionar (OpenCV no disponible)")


def _video(path: Path, n_frames: int = MAX_VIDEO_FRAMES) -> Attachment:
    cv2 = _cv2()
    if cv2 is None:
        return Attachment(path.name, "unsupported", note="Para video instala opencv-python-headless")
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return Attachment(path.name, "unsupported", note="No se pudo abrir el video")
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames: list[tuple[str, str]] = []
        if total > 0:
            idxs = sorted({int(total * (i + 0.5) / n_frames) for i in range(n_frames)})
            for i in idxs:
                cap.set(cv2.CAP_PROP_POS_FRAMES, i)
                ok, frame = cap.read()
                if ok:
                    frames.append(_encode_frame(cv2, frame))
        else:
            i = 0
            while len(frames) < n_frames:
                ok, frame = cap.read()
                if not ok:
                    break
                if i % 30 == 0:
                    frames.append(_encode_frame(cv2, frame))
                i += 1
    finally:
        cap.release()
    if not frames:
        return Attachment(path.name, "unsupported", note="No se pudieron leer fotogramas")
    dur = f", {total / fps:.0f}s" if total and fps else ""
    return Attachment(path.name, "video", images=frames,
                      text=f"(Video: {len(frames)} fotogramas equidistantes{dur})")


def _pdf(path: Path) -> Attachment:
    try:
        import fitz
    except ImportError:
        return Attachment(path.name, "unsupported", note="Instala PyMuPDF para leer PDF")
    parts, size = [], 0
    with fitz.open(path) as doc:
        for n, page in enumerate(doc, 1):
            t = page.get_text()
            parts.append(f"[Página {n}]\n{t}")
            size += len(t)
            if size > MAX_TEXT_CHARS:
                break
    return _text_att(path.name, "\n".join(parts))


def _docx(path: Path) -> Attachment:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return _text_att(path.name, re.sub(r"<[^>]+>", "", xml))


def _text_att(name: str, text: str) -> Attachment:
    note = ""
    if len(text) > MAX_TEXT_CHARS:
        text, note = text[:MAX_TEXT_CHARS], f"truncado a {MAX_TEXT_CHARS} caracteres"
    return Attachment(name, "text", text=text, note=note)


def load_attachment(path: str | Path) -> Attachment:
    p = Path(path)
    try:
        ext = p.suffix.lower()
        if ext in VIDEO_EXT:
            return _video(p)
        if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            return Attachment(p.name, "unsupported", note=f"Más de {MAX_FILE_MB} MB")
        if ext in IMAGE_EXT:
            return _image(p)
        if ext == ".pdf":
            return _pdf(p)
        if ext == ".docx":
            return _docx(p)
        if ext in TEXT_EXT or not ext:
            raw = p.read_bytes()
            if b"\x00" in raw[:4096]:
                return Attachment(p.name, "unsupported", note="Archivo binario")
            return _text_att(p.name, raw.decode("utf-8", errors="replace"))
        return Attachment(p.name, "unsupported", note=f"Formato {ext} no soportado")
    except Exception as e:
        return Attachment(p.name, "unsupported", note=f"Error al leer: {e}")


def build_prompt(user_text: str, atts: list[Attachment]) -> tuple[str, list[tuple[str, str]]]:
    blocks, images = [], []
    for a in atts:
        if a.kind in ("text", "video") and a.text:
            blocks.append(f"--- Archivo: {a.name} ---\n{a.text}\n--- Fin de {a.name} ---")
        images += a.images
    return ("\n\n".join(blocks + [user_text]) if blocks else user_text), images
