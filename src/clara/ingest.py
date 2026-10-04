"""The text of the files people give Clara: text and code as they are, PDF and Word (.docx) documents read on the
server, and .zip archives (a folder, a GitHub repository) unpacked, keeping their text files.

Everything is text in the end: a project (projects.py) stores what `extract` and `unpack` return. What is not
text (images, programs, fonts...) or is not worth reading (dependencies, build output, lock files) is skipped,
with the reason.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

MAX_FILE_CHARS = 1_000_000  # characters of text one file may give (a bigger one is not worth a prompt)
MAX_RAW_BYTES = 30_000_000  # a file in an archive bigger than this is not even read
MAX_UNPACKED_BYTES = 600_000_000  # what one archive may unpack to, in all (a zip bomb stops here)
MAX_PATH = 300

# Folders that hold no source of one's own: dependencies, caches, build output, version control
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "bower_components", "__pycache__", ".venv", "venv", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".gradle", ".idea", ".next", ".nuxt", ".svelte-kit",
    ".parcel-cache", ".turbo", "dist", "build", "target", "out", "coverage", "htmlcov", ".terraform",
    ".dart_tool", "Pods", "DerivedData", ".eggs",
})
IGNORED_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
    "uv.lock", "Pipfile.lock", "go.sum", ".DS_Store", "Thumbs.db", "desktop.ini",
})
IGNORED_SUFFIXES = (".min.js", ".min.css", ".map", ".egg-info", ".pyc", ".pyo", ".lock")
# Not text, whatever their bytes look like at first: skipped without being read
BINARY_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "tif", "tiff", "psd", "heic", "avif",
    "mp3", "wav", "ogg", "flac", "m4a", "aac", "mp4", "mkv", "avi", "mov", "webm", "wmv",
    "exe", "dll", "so", "dylib", "bin", "o", "a", "lib", "obj", "class", "jar", "war", "apk", "msi", "dmg", "iso",
    "woff", "woff2", "ttf", "otf", "eot", "7z", "rar", "gz", "tgz", "bz2", "xz", "tar", "zst",
    "sqlite", "sqlite3", "db", "pkl", "pickle", "npy", "npz", "pt", "pth", "onnx", "h5", "parquet",
    "doc", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "key", "pages", "numbers",
})

LANGUAGES = {
    "py": "python", "pyw": "python", "pyi": "python", "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp", "cxx": "cpp",
    "hpp": "cpp", "md": "markdown", "markdown": "markdown", "rst": "rst", "tex": "latex", "json": "json",
    "yaml": "yaml", "yml": "yaml", "toml": "toml", "ini": "ini", "cfg": "ini", "csv": "csv", "xml": "xml",
    "html": "html", "htm": "html", "css": "css", "scss": "scss", "sql": "sql", "js": "javascript",
    "mjs": "javascript", "cjs": "javascript", "ts": "typescript", "tsx": "tsx", "jsx": "jsx", "java": "java",
    "kt": "kotlin", "go": "go", "rs": "rust", "rb": "ruby", "php": "php", "sh": "bash", "bash": "bash",
    "ps1": "powershell", "bat": "batch", "cs": "csharp", "swift": "swift", "lua": "lua", "r": "r", "scala": "scala",
    "vue": "vue", "svelte": "svelte", "dart": "dart", "m": "objectivec", "zig": "zig", "ex": "elixir",
    "exs": "elixir", "erl": "erlang", "hs": "haskell", "ml": "ocaml", "fs": "fsharp", "clj": "clojure",
    "gradle": "groovy", "groovy": "groovy", "dockerfile": "dockerfile", "makefile": "makefile", "proto": "protobuf",
    "graphql": "graphql", "tf": "hcl", "nix": "nix", "svg": "xml",
}


class IngestError(ValueError):
    """A file that cannot be used: the message says why, for the person."""


@dataclass(frozen=True)
class ExtractedFile:
    path: str  # relative, with "/"
    kind: str  # the language ("python"...), "pdf", "docx" or "" for plain text
    text: str


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


def extension(path: str) -> str:
    name = posixpath.basename(path).lower()
    if name in ("dockerfile", "makefile"):
        return name
    _, dot, ext = name.rpartition(".")
    return ext if dot else ""


def clean_path(path: str) -> str:
    """A safe relative path: forward slashes, no leading slash, no `.` or `..`. IngestError if nothing is left."""
    parts = []
    for part in path.replace("\\", "/").split("/"):
        part = part.strip()
        if part in ("", "."):
            continue
        if part == "..":
            raise IngestError(f"{path}: a path may not go up (..)")
        parts.append(re.sub(r"[\x00-\x1f]", "", part))
    cleaned = "/".join(parts)
    if not cleaned:
        raise IngestError("A file has no name")
    if len(cleaned) > MAX_PATH:
        raise IngestError(f"{cleaned[:60]}…: the path is longer than {MAX_PATH} characters")
    return cleaned


def ignored(path: str) -> str | None:
    """Why a file of a folder or an archive is left out, or None to keep it."""
    parts = path.split("/")
    for folder in parts[:-1]:
        if folder in IGNORED_DIRS or folder.endswith(".egg-info"):
            return f"in {folder}/"
    name = parts[-1]
    if name in IGNORED_FILES or name.lower().endswith(IGNORED_SUFFIXES):
        return "generated file"
    if extension(name) in BINARY_EXTENSIONS:
        return "not a text file"
    return None


# ----------------------------------------------------------------------
# Documents
# ----------------------------------------------------------------------
def pdf_text(data: bytes) -> tuple[str, int]:
    """The text of a PDF's pages, and how many pages it has."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise IngestError("Reading PDF files needs pypdf on the server: pip install pypdf") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise IngestError("This PDF is protected by a password.")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except IngestError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError, AttributeError) as error:
        raise IngestError(f"This file could not be read as a PDF ({error}).") from None
    if not any(pages):
        raise IngestError("This PDF has no text to read (a scanned PDF?).")
    return "\n\n".join(f"[page {n}]\n{page}" for n, page in enumerate(pages, 1) if page), len(pages)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph(element: ElementTree.Element) -> str:
    out = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            out.append(node.text)
        elif node.tag == f"{_W}tab":
            out.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            out.append("\n")
    return "".join(out)


def docx_text(data: bytes) -> str:
    """The text of a Word document: its paragraphs, and its tables as rows of cells separated by `|`."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        raise IngestError("This file could not be read as a Word document (.docx).") from None
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise IngestError("This Word document is damaged.") from None
    body = root.find(f"{_W}body")
    lines: list[str] = []
    for block in body if body is not None else []:
        if block.tag == f"{_W}p":
            lines.append(_paragraph(block))
        elif block.tag == f"{_W}tbl":
            for row in block.iter(f"{_W}tr"):
                cells = [" ".join(_paragraph(p) for p in cell.iter(f"{_W}p")).strip() for cell in row.iter(f"{_W}tc")]
                lines.append("| " + " | ".join(cells) + " |")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise IngestError("This Word document has no text.")
    return text


def decode_text(data: bytes) -> str:
    """Bytes as text (UTF-8, else Windows-1252); IngestError if they look binary."""
    if b"\x00" in data[:8192]:
        raise IngestError("not a text file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return text.removeprefix("﻿")


def extract(path: str, data: bytes) -> ExtractedFile:
    """The text of one file (not an archive). IngestError says why it cannot be used."""
    path = clean_path(path)
    ext = extension(path)
    if ext == "pdf" or data.startswith(b"%PDF"):
        text, _ = pdf_text(data)
        kind = "pdf"
    elif ext == "docx":
        text, kind = docx_text(data), "docx"
    elif ext in BINARY_EXTENSIONS:
        raise IngestError("not a text file")
    else:
        text, kind = decode_text(data), LANGUAGES.get(ext, "")
    if not text.strip():
        raise IngestError("empty")
    if len(text) > MAX_FILE_CHARS:
        raise IngestError(f"too big: {len(text):,} characters of text (at most {MAX_FILE_CHARS:,} per file)")
    return ExtractedFile(path, kind, text)


def unpack(data: bytes, folder: str = "", strip_root: bool = False) -> tuple[list[ExtractedFile], list[Skipped]]:
    """The text files of a .zip archive, put in `folder`. `strip_root` drops the one folder every entry is in
    (GitHub's archives have one: `owner-repo-commit/`)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise IngestError("This is not a .zip archive, or it is damaged.") from None
    files: list[ExtractedFile] = []
    skipped: list[Skipped] = []
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename.replace("\\", "/") for info in entries]
        root = ""
        if strip_root and names:
            first = names[0].split("/", 1)[0]
            if all(name.startswith(first + "/") for name in names):
                root = first + "/"
        unpacked = 0
        for info, name in zip(entries, names):
            name = name[len(root):]
            try:
                path = clean_path(posixpath.join(folder, name) if folder else name)
            except IngestError as error:
                skipped.append(Skipped(name, str(error)))
                continue
            reason = ignored(name)
            if reason is None and extension(path) == "zip":
                reason = "an archive inside the archive"
            if reason is None and info.file_size > MAX_RAW_BYTES:
                reason = f"too big ({info.file_size // 1_000_000} MB)"
            if reason is None and unpacked + info.file_size > MAX_UNPACKED_BYTES:
                reason = "the archive unpacks to too much"
            if reason is not None:
                skipped.append(Skipped(path, reason))
                continue
            try:
                raw = archive.read(info)
                unpacked += len(raw)
                files.append(extract(path, raw))
            except IngestError as error:
                skipped.append(Skipped(path, str(error)))
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as error:
                skipped.append(Skipped(path, f"could not be unpacked ({error})"))
    return files, skipped


def expand(path: str, data: bytes) -> tuple[list[ExtractedFile], list[Skipped]]:
    """One uploaded file: an archive gives its files (in a folder named after it), any other file itself."""
    try:
        path = clean_path(path)
    except IngestError as error:
        return [], [Skipped(path, str(error))]
    if extension(path) == "zip":
        folder = path[: -len(".zip")]
        try:
            return unpack(data, folder)
        except IngestError as error:
            return [], [Skipped(path, str(error))]
    try:
        return [extract(path, data)], []
    except IngestError as error:
        return [], [Skipped(path, str(error))]
