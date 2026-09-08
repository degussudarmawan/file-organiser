"""Category naming: validation and normalisation, not a fixed vocabulary.

Categories are decided per run from the actual contents of the target folder —
the scheme proposer agent reads the unplaceable files and proposes a grouping
that fits them, rather than forcing them into a generic list.

Because a category name is now model-produced and becomes a real folder name
on disk, everything that turns a proposal into a directory goes through
`normalise_category()` first. A name that survives it is safe to join onto the
target path: no separators, no traversal, no control characters, no leading
dot, bounded length. The manager re-checks the final destination anyway, but
sanitising at the source keeps a bad proposal from ever becoming a path.
"""

import re
import unicodedata

# Where a file lands when nothing in the proposed scheme fits it.
FALLBACK_CATEGORY = "Other"

CONFIDENCE_LEVELS = ["high", "medium", "low"]

MAX_CATEGORY_LENGTH = 40

# Anything outside this set is stripped. Deliberately narrow: these names
# become folders, and "&", "-" and "_" cover the realistic cases without
# letting through quotes, separators or shell-significant punctuation.
_UNSAFE = re.compile(r"[^A-Za-z0-9 &_-]")
_WHITESPACE = re.compile(r"\s+")

# Names that are meaningless or dangerous as folders regardless of sanitising.
_RESERVED = {"", ".", "..", "con", "prn", "aux", "nul"}


def normalise_category(raw: str | None) -> str | None:
    """Return a filesystem-safe category name, or None if it can't be one.

    Returning None is meaningful: the caller should treat the proposal as
    unusable and fall back rather than trying to repair it.
    """
    if not raw or not isinstance(raw, str):
        return None

    # Decompose accents to ASCII rather than stripping the letter entirely, so
    # "Facturación" degrades to "Facturacion" instead of "Facturacin".
    name = unicodedata.normalize("NFKD", raw)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))

    # Drop control characters before anything else looks at the string.
    name = "".join(ch for ch in name if ch.isprintable())

    name = _UNSAFE.sub(" ", name)
    name = _WHITESPACE.sub(" ", name).strip(" ._-")

    if not name or name.lower() in _RESERVED:
        return None

    if len(name) > MAX_CATEGORY_LENGTH:
        # Cut on a word boundary where possible so the folder still reads.
        name = name[:MAX_CATEGORY_LENGTH].rsplit(" ", 1)[0] or name[:MAX_CATEGORY_LENGTH]
        name = name.strip(" ._-")

    return name or None


def dedupe_categories(names: list[str]) -> list[str]:
    """Normalise a proposed scheme and collapse case-insensitive duplicates.

    A model asked for a grouping will occasionally return "Invoices" and
    "invoices", or the same name twice. First spelling wins; order is kept so
    the proposal reads the way the agent ordered it.
    """
    seen: dict[str, str] = {}
    for raw in names:
        name = normalise_category(raw)
        if name is None:
            continue
        seen.setdefault(name.casefold(), name)
    return list(seen.values())


def is_safe_category(name: str) -> bool:
    """True if `name` is already a normalised, safe category name."""
    return normalise_category(name) == name
