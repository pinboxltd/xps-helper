"""Bump the version in both files, or fail loudly.

Two failures this replaces:

1. The original ad-hoc bump replaced ``"0.7.1"`` with ``"0.7.2"`` when the file
   still said ``"0.7.0"``, matched nothing, and reported success. Two published
   releases then misreported their own version.
2. A naive ``pattern.replace("VERSION", ...)`` also rewrote the identifier
   ``APP_VERSION`` itself, producing ``APP_0.7.4 = "0.7.4"`` and a syntax error.

So this matches the *quoted value only* and refuses to claim success unless both
files were actually changed to the requested version.
"""

import re
import sys

ROOT = r"C:\Users\Stark-Laptop\Desktop\XPS-Helper"
CONSTANTS = ROOT + r"\src\xpshelper\constants.py"
PYPROJECT = ROOT + r"\pyproject.toml"

#: Anchored on the full assignment, so the identifier cannot be touched.
CONSTANTS_PATTERN = re.compile(r'(?m)^APP_VERSION = "([^"]+)"')
PYPROJECT_PATTERN = re.compile(r'(?m)^version = "([^"]+)"')


def bump(path: str, pattern: re.Pattern, replacement: str) -> str:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if not pattern.search(text):
        raise SystemExit(f"ERROR: no version assignment found in {path}")
    updated = pattern.sub(replacement, text, count=1)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    found = pattern.search(updated).group(1)
    print(f"  {path} -> {found}")
    return found


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: bump_version.py X.Y.Z")
    version = sys.argv[1]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"not a release version: {version}")

    got_constants = bump(CONSTANTS, CONSTANTS_PATTERN, f'APP_VERSION = "{version}"')
    got_pyproject = bump(PYPROJECT, PYPROJECT_PATTERN, f'version = "{version}"')

    if got_constants != version or got_pyproject != version:
        raise SystemExit(
            f"ERROR: versions did not take: constants={got_constants} "
            f"pyproject={got_pyproject}, wanted {version}"
        )
    print(f"  both files now say {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
