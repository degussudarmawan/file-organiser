"""The fast rule-based first pass, carried over from file_organizer.py.

This is deliberately dumb and deliberately fast: a filename/extension lookup
table, no I/O, no network, no model. The manager runs this itself for every
file it scans. Anything this pass cannot place confidently is what gets
delegated to the classifier agent.

NOTE: the original file_organizer.py was not in the workspace when this was
built, so the table below is a reconstruction of the categories described in
the brief. Replace EXTENSION_TABLE / FILENAME_RULES with the originals when
you have them — nothing else in the codebase needs to change.
"""

from dataclasses import dataclass
from pathlib import PurePath

# category -> extensions that unambiguously belong to it.
EXTENSION_TABLE = {
    "Images": [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
        ".heic", ".heif", ".tiff", ".tif", ".ico", ".raw", ".cr2", ".nef",
    ],
    "Videos": [
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg",
        ".mpeg", ".wmv", ".flv", ".3gp",
    ],
    "Audio": [
        ".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg", ".oga",
        ".aiff", ".aif", ".wma", ".opus",
    ],
    "Documents": [
        ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md",
        ".markdown", ".tex", ".epub", ".mobi", ".pages",
    ],
    "Spreadsheets": [".xls", ".xlsx", ".xlsm", ".csv", ".tsv", ".ods", ".numbers"],
    "Presentations": [".ppt", ".pptx", ".key", ".odp"],
    "Archives": [
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".rar",
        ".7z", ".iso", ".zipx",
    ],
    "Code": [
        ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".kt",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rb", ".rs", ".php",
        ".swift", ".sh", ".zsh", ".bash", ".pl", ".r", ".sql", ".html",
        ".htm", ".css", ".scss", ".ipynb", ".lua", ".vim",
    ],
    "Data": [
        ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini",
        ".sqlite", ".sqlite3", ".db", ".parquet", ".avro", ".plist",
    ],
    "Design": [".psd", ".ai", ".sketch", ".fig", ".xd", ".indd", ".afdesign", ".afphoto"],
    "Fonts": [".ttf", ".otf", ".woff", ".woff2", ".eot"],
    "Installers": [".dmg", ".pkg", ".exe", ".msi", ".deb", ".rpm", ".appimage"],
}

# Flattened lookup: ".png" -> "Images". Built once at import.
EXTENSION_RULES = {
    ext: category
    for category, extensions in EXTENSION_TABLE.items()
    for ext in extensions
}

# Filename substring rules, checked BEFORE the extension table so a
# high-signal name can override the generic extension mapping. Lowercased
# substring -> category. First match wins, so order matters.
FILENAME_RULES = [
    ("screenshot", "Screenshots"),
    ("screen shot", "Screenshots"),
    ("screen_shot", "Screenshots"),
    ("cleanshot", "Screenshots"),
]


@dataclass(frozen=True)
class RuleMatch:
    """A decision the rule pass made on its own."""

    category: str
    reason: str


def classify_by_rules(filename: str) -> RuleMatch | None:
    """Return a category for `filename`, or None if the rules can't place it.

    Returning None is the signal that this file needs to go to a delegate
    agent — it is never a guess and never a fallback.
    """
    lowered = filename.lower()

    for needle, category in FILENAME_RULES:
        if needle in lowered:
            return RuleMatch(category, f"filename contains {needle!r}")

    suffix = PurePath(lowered).suffix
    if suffix and suffix in EXTENSION_RULES:
        return RuleMatch(EXTENSION_RULES[suffix], f"extension {suffix} is a known type")

    return None


def known_extensions() -> int:
    """Small helper used by the /health endpoint to describe the rule pass."""
    return len(EXTENSION_RULES)


def rule_categories() -> list[str]:
    """Every category this table can emit.

    Categories are otherwise decided per run, but these names are fixed by the
    rule pass. They're handed to the scheme proposer as "already in use" so it
    reuses them instead of coining a synonym for a folder we're already
    filling.
    """
    names = set(EXTENSION_TABLE) | {category for _, category in FILENAME_RULES}
    return sorted(names)
