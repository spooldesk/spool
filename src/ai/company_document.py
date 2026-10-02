"""Bounded, dependency-free extraction of company reference documents."""
from io import BytesIO
from pathlib import PurePath
import re
import zipfile
from xml.etree import ElementTree

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 24000


class DocumentError(ValueError):
    pass


def extract_document(filename: str, content: bytes) -> str:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise DocumentError("document_size")
    suffix = PurePath(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            result = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise DocumentError("document_utf8") from exc
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if (len(entries) > 256 or sum(e.file_size for e in entries) > 8 * 1024 * 1024
                        or len({e.filename for e in entries}) != len(entries)
                        or any(e.flag_bits & 1 or "vbaproject" in e.filename.lower() for e in entries)):
                    raise DocumentError("document_invalid")
                # Read exactly one bounded member; never extract paths or follow links.
                raw = archive.read("word/document.xml")
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper() or b"\x00" in raw:
                    raise DocumentError("document_invalid")
                root = ElementTree.fromstring(raw)
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                lines = []
                for paragraph in root.iter(ns + "p"):
                    lines.append("".join(
                        node.text or "" if node.tag == ns + "t" else "\t" if node.tag == ns + "tab" else "\n"
                        for node in paragraph.iter() if node.tag in {ns + "t", ns + "tab", ns + "br"}
                    ))
                result = "\n".join(lines)
        except (zipfile.BadZipFile, KeyError, RuntimeError, ElementTree.ParseError, EOFError, NotImplementedError) as exc:
            raise DocumentError("document_invalid") from exc
    else:
        raise DocumentError("document_format")
    if "\x00" in result:
        raise DocumentError("document_invalid")
    result = re.sub(r"\n{3,}", "\n\n", result.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not result:
        raise DocumentError("document_empty")
    if len(result) > MAX_TEXT_CHARS:
        raise DocumentError("document_text_size")
    return result
