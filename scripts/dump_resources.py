"""List embedded manifest resources in a .NET assembly.

If Dell defines the BDat/DoBFn WMI class from an embedded BMOF/MOF, the resource
manifest will name it. The metadata `ManifestResource` table points at the #Strings
heap, so scanning that heap for resource-looking names is enough to find it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dump_net_strings import streams  # noqa: E402


def main() -> int:
    for arg in sys.argv[1:]:
        path = Path(arg)
        heaps = streams(path.read_bytes())
        strings = heaps.get("#Strings", b"")
        names = [n.decode("utf-8", "replace") for n in strings.split(b"\0") if n]
        print(f"===== {path.name} =====")
        print(f"#Strings names: {len(names)}")

        hits = [
            n
            for n in names
            if any(k in n.lower() for k in ("resource", "bmof", "mof", "bdat", "dofn", "wmi"))
        ]
        if hits:
            print("resource/WMI-looking names:")
            for n in sorted(set(hits)):
                print("   ", n)
        else:
            print("(no resource/WMI-looking names)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
