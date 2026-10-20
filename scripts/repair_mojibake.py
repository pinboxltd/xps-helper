"""Repair double-encoded UTF-8 in a text file.

This happened because the file was written by PowerShell's Set-Content, which read
existing UTF-8 bytes as cp1252 and re-encoded them. The damage is visible as
sequences like 'â€”' where an em dash belongs.

The repair reverses exactly that: each run of characters that could only have come
from mis-decoded UTF-8 bytes is re-encoded to cp1252 and decoded back as UTF-8. A
run that does not convert cleanly is left untouched, so genuine text cannot be
damaged by the fix.
"""

import pathlib
import re
import sys

# Characters that appear when UTF-8 bytes are read as cp1252.
#
# The 0x80-0xBF byte range maps to a scattered set of cp1252 code points, not only
# U+0080-U+00BF: 0x88 becomes U+02C6 (ˆ), 0x98 U+02DC, 0x89 U+2030, and so on. A
# run split by one of those is skipped, which is how a lone 'âˆ’' survived the
# first repair.
SUSPICIOUS = (
    "\u00c2\u00c3\u00e2"          # Â Ã â
    "\u0080-\u009f"               # cp1252 controls
    "\u00a0-\u00bf"               # non-breaking space through ¿
    "\u02c6\u02c7\u02d8-\u02dd"   # ˆ ˇ ˘ ˙ ˚ ˛ ˜ ˝
    "\u0152\u0153\u0160\u0161"    # Œ œ Š š
    "\u0178\u017d\u017e"          # Ÿ Ž ž
    "\u0192"                      # ƒ
    "\u2013-\u2122"               # dashes, quotes, dagger, bullet, euro, ellipsis
    "\u2030\u2039\u203a\u203e"    # ‰ ‹ › ‾
    "\u20ac"                      # €
    "\u2190-\u2195"               # arrows that come from 0x86-0x8F pairs
)


def repair(text: str) -> tuple[str, int]:
    pattern = re.compile(f"[{SUSPICIOUS}]{{2,}}")
    converted = 0

    def fix(match: re.Match) -> str:
        nonlocal converted
        run = match.group(0)
        try:
            recovered = run.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return run
        if not recovered or recovered == run:
            return run
        converted += 1
        return recovered

    return pattern.sub(fix, text), converted


def main() -> int:
    for name in sys.argv[1:]:
        path = pathlib.Path(name)
        raw = path.read_bytes()
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig", "replace")
        fixed, count = repair(text)
        if had_bom or count:
            path.write_text(fixed, encoding="utf-8", newline="\n")
        print(f"  {path.name}: repaired {count} run(s); bom_removed={had_bom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
