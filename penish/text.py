import re
import unicodedata

# Em dash and horizontal bar always become `--`; an en dash only when it is
# acting as a spaced dash, so numeric ranges like 5-7 survive untouched.
_SPACED_EN = re.compile(r"(?<=\s)\u2013(?=\s)")


def no_em_dash(s: str) -> str:
    s = s.replace("\u2014", "--").replace("\u2015", "--")
    return _SPACED_EN.sub("--", s)


def slugify(s: str, max_len: int = 60) -> str:
    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    # Trim after cutting so a truncation never leaves a dangling hyphen.
    return slug[:max_len].strip("-")
