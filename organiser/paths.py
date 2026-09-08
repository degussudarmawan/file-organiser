"""Path safety and the local state directory.

Every rule in the brief that is about *not destroying things* lives here so
there is one place to audit: which folders are reachable at all, target
validation, top-level-only enforcement, and non-overwriting destination
naming.

This module deliberately imports nothing else from the package. `config.py`
imports *this* one, so a dependency back the other way would be a cycle — and
more importantly, the reachability boundary should not be able to fail open
because the provider configuration is broken. It reads the config file itself,
for the one key it cares about.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = ".file-organiser-agent"

# The folders this tool is actually for. Conservative rather than exhaustive:
# a default that reaches a user's whole home directory is not a boundary, it's
# a formality. Anything outside these has to be named deliberately.
DEFAULT_ROOT_NAMES = ("Downloads", "Desktop", "Documents")

ROOTS_ENV = "FILE_ORGANISER_ALLOWED_ROOTS"


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


# ---- Which folders are reachable at all -------------------------------------


@dataclass(frozen=True)
class RootPolicy:
    """The set of folders this tool may touch, and where that came from.

    `source` is carried alongside the roots because a security boundary you
    cannot explain is one nobody will trust. Every interface that reports
    configuration prints it.
    """

    roots: tuple[Path, ...]
    source: str

    def describe(self) -> str:
        if not self.roots:
            return f"nothing is reachable ({self.source})"
        listed = ", ".join(str(r) for r in self.roots)
        return f"{listed}  ({self.source})"

    def permits(self, target: Path) -> bool:
        """True if `target` is one of the roots or sits inside one.

        `target` must already be resolved. Containment is a path comparison,
        never a string prefix: "/Users/me/Downloads-old" starts with
        "/Users/me/Downloads" as text but is a different directory, and
        allowing it would be a real escape.
        """
        return any(target == root or target.is_relative_to(root) for root in self.roots)


def _split_roots(raw: str) -> list[Path]:
    return [
        Path(os.path.expanduser(part.strip())).resolve()
        for part in raw.split(os.pathsep)
        if part.strip()
    ]


def root_policy() -> RootPolicy:
    """Resolve the allowlist: env var, then the config file, then the default.

    Same precedence as every other setting, so there's one rule to learn.
    """
    env = os.environ.get(ROOTS_ENV)
    if env is not None and env.strip():
        return RootPolicy(tuple(_split_roots(env)), f"${ROOTS_ENV}")

    config_file = state_dir() / "config.json"
    if config_file.exists():
        try:
            data = json.loads(config_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # FAIL CLOSED. A config we cannot read is not permission to fall
            # back to something broader — the user may well have written a
            # narrower list than the default, and quietly widening it is the
            # exact failure this whole module exists to prevent.
            raise UnsafePath(
                f"cannot read {config_file} ({exc}), so no folder is reachable. "
                "Fix the file, or set "
                f"{ROOTS_ENV} to name the folders explicitly."
            ) from exc
        if isinstance(data, dict) and "allowed_roots" in data:
            entries = data["allowed_roots"]
            if isinstance(entries, str):
                entries = [entries]
            if not isinstance(entries, list) or not all(
                isinstance(e, str) for e in entries
            ):
                raise UnsafePath(
                    f'"allowed_roots" in {config_file} must be a list of folder paths'
                )
            roots = [Path(os.path.expanduser(e)).resolve() for e in entries]
            return RootPolicy(tuple(roots), f"allowed_roots in {config_file}")

    home = Path.home()
    default = tuple(
        p for p in ((home / name).resolve() for name in DEFAULT_ROOT_NAMES) if p.is_dir()
    )
    return RootPolicy(default, "built-in default")


def validate_target_dir(raw: str, policy: RootPolicy | None = None) -> Path:
    """Resolve a user-supplied target folder, or raise UnsafePath.

    `policy` defaults to the configured allowlist rather than to "anything",
    so a caller that forgets to pass one gets the boundary, not a hole.
    """
    if not raw or not raw.strip():
        raise UnsafePath("no target folder given")

    # resolve() first, and check the RESOLVED path. A symlink inside an allowed
    # root that points somewhere else entirely is the obvious way out of a
    # boundary like this, and resolving collapses it — along with any "..".
    target = Path(os.path.expanduser(raw.strip())).resolve()

    if not target.exists():
        raise UnsafePath(f"{target} does not exist")
    if not target.is_dir():
        raise UnsafePath(f"{target} is not a folder")

    # Defence in depth, kept even though the allowlist now implies it: these
    # stay refused even if somebody allowlists them outright.
    if target == Path("/") or target == Path.home():
        raise UnsafePath(f"{target} is too broad a target for this tool")

    policy = policy or root_policy()
    if not policy.permits(target):
        raise UnsafePath(
            f"{target} is outside the folders this tool may touch. "
            f"Allowed: {policy.describe()}"
        )

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
