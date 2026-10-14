"""Extract .NET #US (user string) heap from a managed assembly.

Dell's assemblies are .NET Framework; their user strings (registry paths,
message text, enum names) live uncompressed in the #US stream. This pulls them
out so the policy/registry contract can be read directly instead of guessed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def streams(data: bytes) -> dict[str, bytes]:
    """Return the metadata heap streams, keyed by name."""
    out: dict[str, bytes] = {}
    # Locate the CLI header via the PE optional data directory 14.
    pe = int.from_bytes(data[0x3C:0x40], "little")
    magic = int.from_bytes(data[pe + 24 : pe + 26], "little")
    is64 = magic == 0x20B
    opt_size = int.from_bytes(data[pe + 20 : pe + 22], "little")
    dd = pe + 24 + (112 if is64 else 96)
    cor_rva = int.from_bytes(data[dd + 14 * 8 : dd + 14 * 8 + 4], "little")
    if cor_rva == 0:
        return out

    num_sec = int.from_bytes(data[pe + 6 : pe + 8], "little")
    sec = pe + 24 + opt_size
    sections = []
    for i in range(num_sec):
        s = sec + i * 40
        sections.append(
            (
                int.from_bytes(data[s + 12 : s + 16], "little"),
                int.from_bytes(data[s + 8 : s + 12], "little"),
                int.from_bytes(data[s + 20 : s + 24], "little"),
            )
        )

    def rva_to_off(rva: int) -> int:
        for va, vsize, raw in sections:
            if va <= rva < va + max(vsize, 1):
                return raw + (rva - va)
        return -1

    cor = rva_to_off(cor_rva)
    if cor < 0:
        return out
    md_rva = int.from_bytes(data[cor + 8 : cor + 12], "little")
    md = rva_to_off(md_rva)
    if md < 0:
        return out

    # Metadata root: signature, version, then stream headers.
    ver_len = int.from_bytes(data[md + 12 : md + 16], "little")
    p = md + 16 + ver_len
    p += 2 + 2  # flags, stream count
    count = int.from_bytes(data[md + 16 + ver_len + 2 : md + 16 + ver_len + 4], "little")
    for _ in range(count):
        off = int.from_bytes(data[p : p + 4], "little")
        size = int.from_bytes(data[p + 4 : p + 8], "little")
        name_end = data.index(b"\0", p + 8)
        name = data[p + 8 : name_end].decode("ascii", "replace")
        p = name_end + 1
        p = (p + 3) & ~3
        out[name] = data[md + off : md + off + size]
    return out


def user_strings(us: bytes) -> list[str]:
    """Parse the #US heap into a list of strings."""
    found: list[str] = []
    i = 1  # first byte is a 0 sentinel
    while i < len(us):
        # Compressed length: 1, 2 or 4 bytes.
        b0 = us[i]
        if b0 & 0x80 == 0:
            length, i = b0, i + 1
        elif b0 & 0xC0 == 0x80:
            length = ((b0 & 0x3F) << 8) | us[i + 1]
            i += 2
        else:
            length = (
                ((b0 & 0x1F) << 24)
                | (us[i + 1] << 16)
                | (us[i + 2] << 8)
                | us[i + 3]
            )
            i += 4
        if length == 0:
            continue
        blob = us[i : i + length]
        i += length
        # Trailing byte is a flag; text is UTF-16LE.
        text = blob[:-1].decode("utf-16-le", "replace")
        if text:
            found.append(text)
    return found


def interesting(strings: list[str]) -> list[str]:
    """Keep the strings that describe the policy contract."""
    patterns = [
        r"SOFTWARE\\",
        r"Policies",
        r"Thermal",
        r"RegistryPath",
        r"CommandPowerManager",
        r"^(Quiet|Cool|Optimized|UltraPerformance|Balanced)$",
        r"Policy",
        r"ValuePath",
        r"FeatureEnablement",
    ]
    rx = re.compile("|".join(patterns))
    seen = set()
    out = []
    for s in strings:
        if rx.search(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def main() -> int:
    for arg in sys.argv[1:]:
        path = Path(arg)
        data = path.read_bytes()
        heaps = streams(data)
        print(f"===== {path.name} =====")
        print("heaps:", ", ".join(sorted(heaps)) or "(none found)")

        us = heaps.get("#US")
        if us:
            all_strings = user_strings(us)
            print(f"#US user strings: {len(all_strings)}")
            for s in interesting(all_strings)[:120]:
                print("   us:", s.replace("\n", "\\n"))

        st = heaps.get("#Strings")
        if st:
            names = [n.decode("utf-8", "replace") for n in st.split(b"\0") if n]
            print(f"#Strings names: {len(names)}")
            for s in interesting(names)[:160]:
                print("   st:", s)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
