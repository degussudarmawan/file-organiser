"""Path safety and the local state directory.

Every rule in the brief that is about *not destroying things* lives here so
there is one place to audit: target validation, top-level-only enforcement,
and non-overwriting destination naming.
"""

import os
import re
from pathlib import Path

APP_DIR_NAME = ".file-organiser-agent"


class UnsafePath(ValueError):
    """Raised when a path fails a safety check. Always fatal for that item."""


def state_dir() -> Path:
    """Where plans, the undo log and the first-run acknowledgements live."""
    root = Path(os.environ.get("FILE_ORGANISER_STATE_DIR", Path.home() / APP_DIR_NAME))
    root.mkdir(parents=True, exist_ok=True)
    (root / "plans").mkdir(exist_ok=True)
    return root


def plans_dir() -> Path:
    return state_dir() / "plans"


def validate_target_dir(raw: str) -> Path:
    """Resolve a user-supplied target folder, or raise UnsafePath."""
    if not raw or not raw.strip():
        raise UnsafePath("no target folder given")

    target = Path(os.path.expanduser(raw.strip())).resolve()

    if not target.exists():
        raise UnsafePath(f"{target} does not exist")
    if not target.is_dir():
        raise UnsafePath(f"{target} is not a folder")

    # Refuse the obviously catastrophic targets. This tool only ever moves
    # files, but pointing it at / or at $HOME itself is never what you meant.
    if target == Path("/") or target == Path.home():
        raise UnsafePath(f"{target} is too broad a target for this tool")

    return target


def is_direct_child(path: Path, parent: Path) -> bool:
    """True only if `path` sits directly in `parent` — not in a subfolder.

    This is the enforcement point for "never touch subfolders in v1".
    """
    try:
        return path.resolve().parent == parent.resolve()
    except OSError:
        return False


_SUFFIX_RE = re.compile(r"^(?P<stem>.*?)(?P<n>_\d+)?$")


def unique_destination(destination: Path, reserved: set[Path] | None = None) -> Path:
    """Return a path that does not exist and is not already claimed.

    Appends `_1`, `_2`, ... before the extension. `reserved` holds destinations
    already claimed earlier in the same plan, so two files with the same name
    landing in the same category don't collide with each other.
    """
    reserved = reserved or set()

    if not destination.exists() and destination not in reserved:
        return destination

    stem = destination.stem
    suffix = destination.suffix
    parent = destination.parent

    # Don't stack suffixes: report_1.pdf becomes report_2.pdf, not report_1_1.pdf.
    match = _SUFFIX_RE.match(stem)
    base = match.group("stem") if match and match.group("n") else stem

    counter = 1
    while True:
        candidate = parent / f"{base}_{counter}{suffix}"
        if not candidate.exists() and candidate not in reserved:
            return candidate
        counter += 1
        if counter > 9999:  # pathological; better to fail than spin
            raise UnsafePath(f"could not find a free name for {destination}")


def should_skip(entry: Path) -> str | None:
    """Reason to skip this directory entry, or None to process it.

    Top-level files only: directories, symlinks and dotfiles are left alone.
    """
    name = entry.name
    if name.startswith("."):
        return "hidden file"
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir():
        return "folder (v1 only handles top-level files)"
    if not entry.is_file():
        return "not a regular file"
    return None
