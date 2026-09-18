"""Pure Topic/Entity name normalization (#30); no Django, models or settings.

Normalization makes trivial variants one row — "Central Bank", "central bank"
and "Central  Bank" share a key — and nothing more. It is not entity
resolution: "IBM" and "International Business Machines" stay different.

    normalize_name: NFKC, casefold, every punctuation or symbol character
                    becomes a space, whitespace collapsed, trimmed.
    slugify:        normalize_name, accents removed, ASCII letters and digits
                    kept, every other run becomes one hyphen, trimmed.
"""

import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def normalize_name(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    spaced = "".join(
        " " if unicodedata.category(char)[0] in {"P", "S"} else char for char in folded
    )
    return " ".join(spaced.split())


def slugify(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", normalize_name(text))
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NON_SLUG.sub("-", ascii_text.encode("ascii", "ignore").decode()).strip("-")
