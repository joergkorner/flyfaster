"""Validate a submitted zip against the folder logic:

- one or many day folders, named  YYYY_MM_DD  (also YYYY-MM-DD, single-digit
  month/day fine), optionally with a suffix — e.g. a second folder for the same
  day (2026_06_18_2) or a race start time (2026_06_18_UTC1000)
- each day folder contains the .igc files of that day's pilots
- a single wrapping root folder (how most zip tools archive a directory) is fine
- __MACOSX / .DS_Store / hidden junk is ignored

Returns a summary dict or raises ValidationError with a user-readable message.
"""
from __future__ import annotations
import io, re, zipfile
from . import config

DAY_RE = re.compile(r"^\d{4}[_-]\d{1,2}[_-]\d{1,2}([_-].*)?$")
JUNK = ("__MACOSX/", ".DS_Store")


class ValidationError(Exception):
    pass


def _clean_entries(zf: zipfile.ZipFile) -> list[str]:
    names = []
    for n in zf.namelist():
        if any(j in n for j in JUNK):
            continue
        parts = [p for p in n.split("/") if p]
        if any(p.startswith(".") for p in parts):
            continue
        names.append(n)
    return names


def _strip_root(names: list[str]) -> tuple[list[str], str]:
    """If everything lives under one root folder, strip it."""
    roots = {n.split("/", 1)[0] for n in names if "/" in n}
    loose = [n for n in names if "/" not in n and not n.endswith("/")]
    if len(roots) == 1 and not loose:
        root = roots.pop()
        if DAY_RE.match(root):      # the single root IS a day folder, not a wrapper
            return names, ""
        return [n[len(root) + 1:] for n in names if len(n) > len(root) + 1], root
    return names, ""


def day_file_entries(zf: zipfile.ZipFile) -> tuple[list[tuple[str, str, str]], list[str], set[str], str]:
    """Single source of truth for reading a submission zip.
    Returns (entries, stray_igc, bad_folders, root) where entries are
    (name_in_zip, day_folder, filename) for every usable IGC file."""
    names, root = _strip_root(_clean_entries(zf))
    prefix = (root + "/") if root else ""
    entries, stray_igc, bad_folders = [], [], set()
    for n in names:
        if n.endswith("/"):
            continue
        parts = [p for p in n.split("/") if p]
        if not parts[-1].lower().endswith(".igc"):
            continue
        if len(parts) == 1:
            stray_igc.append(parts[0])
            continue
        day, fname = parts[0], parts[-1]
        if DAY_RE.match(day):
            entries.append((prefix + n, day, fname))
        else:
            bad_folders.add(day)
    return entries, stray_igc, bad_folders, root


def validate_zip(data: bytes) -> dict:
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"The zip is larger than the {config.MAX_UPLOAD_MB} MB limit.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        bad = zf.testzip()
        if bad:
            raise ValidationError(f"The zip seems corrupt (first bad file: {bad}).")
    except zipfile.BadZipFile:
        raise ValidationError("That file is not a valid zip archive.")

    entries, stray_igc, bad_folders, root = day_file_entries(zf)
    days: dict[str, int] = {}
    for _, day, _fname in entries:
        days[day] = days.get(day, 0) + 1

    if stray_igc:
        raise ValidationError(
            "Some IGC files sit at the top level of the zip instead of inside a day folder "
            f"(e.g. {stray_igc[0]}). Please put every flight into a folder named like 2026_07_09.")
    if bad_folders and not days:
        raise ValidationError(
            f"No day folders found. Folders must be named YEAR_MONTH_DAY, e.g. 2026_07_09 "
            f"(found instead: {', '.join(sorted(bad_folders)[:3])}).")
    if not days:
        raise ValidationError("The zip contains no .igc files inside day folders.")
    if len(days) > config.MAX_DAYS_PER_ZIP:
        raise ValidationError(f"Too many day folders ({len(days)}; limit {config.MAX_DAYS_PER_ZIP}).")
    n_files = sum(days.values())
    if n_files > config.MAX_FILES_PER_ZIP:
        raise ValidationError(f"Too many IGC files ({n_files}; limit {config.MAX_FILES_PER_ZIP}).")

    return {
        "root": root,
        "days": dict(sorted(days.items())),
        "n_days": len(days),
        "n_files": n_files,
        "skipped_folders": sorted(bad_folders),
    }
