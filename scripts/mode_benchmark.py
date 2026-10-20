"""Time a fixed amount of CPU work in each thermal mode.

This is the test that answers "does it actually do anything", without needing a
driver or knowing any units.

Reasoning: a thermal mode can only change behaviour by changing a power or thermal
limit, or a fan curve. If it changes a power limit, the CPU cannot sustain the same
clock, so **the same fixed workload takes measurably longer**. If every mode
completes the work in the same time, then nothing about CPU performance changed --
regardless of what any sensor claims.

The workload is deterministic integer arithmetic on one thread: no allocation, no
I/O, no dependence on clocks or wall-clock time inside the loop. The loop count is
calibrated once at the start so each run takes a few seconds, long enough for a
sustained power limit to matter rather than the initial boost.

Results go to JSON so the caller is not limited by a helper timeout.
"""

import json
import multiprocessing as mp
import sys
import time

sys.path.insert(0, r"C:\Users\Stark-Laptop\Desktop\XPS-Helper\src")


def work(count: int) -> int:
    """Fixed integer work. Deliberately boring and deterministic."""
    total = 0
    for i in range(count):
        total = (total + i * i) % 1000003
    return total


def calibrate() -> int:
    """Loop count that takes roughly six seconds on this machine."""
    probe = 2_000_000
    start = time.perf_counter()
    work(probe)
    elapsed = time.perf_counter() - start
    per_iter = elapsed / probe
    return max(probe, int(6.0 / per_iter))


def main() -> int:
    from xpshelper import thermal
    from xpshelper.backends.wmi import WmiTelemetryBackend

    out_path = sys.argv[1]
    mode_order = sys.argv[2].split(",")

    count = calibrate()

    results = []
    for mode in mode_order:
        thermal.set_thermal_mode(mode)
        time.sleep(3)  # let the firmware adopt it

        start = time.perf_counter()
        work(count)
        elapsed = time.perf_counter() - start

        temps = []
        try:
            raw = WmiTelemetryBackend().refresh()
            temps = [z.get("Temperature") for z in raw.esif_zones if z.get("Temperature")]
        except Exception:  # noqa: BLE001
            pass

        results.append(
            {
                "mode": mode,
                "seconds": round(elapsed, 3),
                "iterations": count,
                "hottest_c": max(temps) if temps else None,
            }
        )
        print(json.dumps(results[-1]), flush=True)
        time.sleep(8)  # cool down before the next mode

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
