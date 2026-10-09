"""Upload intake: type detection, streamed save with a size cap, and safety checks.

Nothing here parses geometry; it only decides whether the bytes are safe and
well-formed enough to hand to GDAL.
"""

import hashlib
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.config import Settings
from app.errors import ApiError
from app.models import FileType

CHUNK = 1024 * 1024
_SHP_PARTS_REQUIRED = (".shx", ".dbf")
_DTD_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass
class SavedUpload:
    path: Path
    size: int
    sha256: str


def detect_file_type(filename: str) -> FileType:
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        return FileType.SHAPEFILE
    if suffix == ".kml":
        return FileType.KML
    raise ApiError(415, "unsupported_file_type", "Upload a .zip containing a Shapefile, or a .kml file.")


def save_stream(src: BinaryIO, dest: Path, max_bytes: int) -> SavedUpload:
    """Copy in chunks while hashing; abort as soon as the size cap is crossed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := src.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(413, "file_too_large", f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.")
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ApiError(400, "empty_file", "The uploaded file is empty.")
    return SavedUpload(dest, size, digest.hexdigest())


def validate_kml(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(64 * 1024)
    if head.startswith(b"PK"):
        raise ApiError(415, "unsupported_file_type", "Looks like a KMZ/zip archive; upload a plain .kml file.")
    if b"<kml" not in head.lower():
        raise ApiError(415, "invalid_kml", "File does not look like KML (no <kml> root element).")
    # Entity declarations are the vehicle for billion-laughs / XXE; KML never needs them.
    if _DTD_RE.search(head):
        raise ApiError(400, "invalid_kml", "DOCTYPE/ENTITY declarations are not allowed in KML uploads.")
    return path.resolve().as_posix()


def _is_junk(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return any(p == "__MACOSX" for p in parts) or PurePosixPath(name).name.startswith("._")


def validate_shapefile_zip(path: Path, settings: Settings) -> str:
    """Check the archive and return the GDAL /vsizip/ path of its single .shp."""
    if not zipfile.is_zipfile(path):
        raise ApiError(400, "invalid_zip", "File is not a valid zip archive.")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ApiError(400, "invalid_zip", f"Corrupt zip archive: {exc}") from exc

    if len(infos) > settings.max_zip_entries:
        raise ApiError(400, "invalid_zip", f"Zip has more than {settings.max_zip_entries} entries.")

    total = 0
    names: dict[str, str] = {}  # lowercase -> original
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
            raise ApiError(400, "invalid_zip", f"Unsafe path in zip: {name!r}")
        if info.flag_bits & 0x1:
            raise ApiError(400, "invalid_zip", "Encrypted zip entries are not supported.")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ApiError(400, "invalid_zip", f"Symlinks are not allowed in zip: {name!r}")
        if info.is_dir():
            continue
        total += info.file_size
        if total > settings.max_uncompressed_bytes:
            raise ApiError(413, "file_too_large", "Zip contents exceed the uncompressed size limit.")
        if (
            info.file_size > CHUNK
            and info.compress_size > 0
            and info.file_size / info.compress_size > settings.max_compression_ratio
        ):
            raise ApiError(400, "invalid_zip", f"Suspicious compression ratio for {name!r}.")
        if not _is_junk(name):
            names[name.lower()] = name

    shps = [orig for low, orig in names.items() if low.endswith(".shp")]
    if not shps:
        raise ApiError(400, "invalid_shapefile", "No .shp file found in the zip.")
    if len(shps) > 1:
        raise ApiError(400, "invalid_shapefile", f"Zip must contain exactly one shapefile; found {len(shps)}.")

    shp = shps[0]
    stem = shp[:-4].lower()
    missing = [ext for ext in _SHP_PARTS_REQUIRED if stem + ext not in names]
    if missing:
        raise ApiError(400, "invalid_shapefile", f"Shapefile is missing required component(s): {', '.join(missing)}")

    return f"/vsizip/{path.resolve().as_posix()}/{shp}"
