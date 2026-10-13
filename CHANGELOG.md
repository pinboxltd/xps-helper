# Changelog

## v0.2.0

Fixes two problems reported from a real XPS 13 7390 2-in-1.

### Fixed
- **A PowerShell console window flashed on every refresh and every setting
  change.** The GUI is built without a console (`console=False`), so Windows
  allocated a new console window for each PowerShell child process the app
  spawns for WMI access. All PowerShell launches now pass `CREATE_NO_WINDOW` plus
  a hidden `STARTUPINFO`. This affected the GUI worst, but the CLI too.

- **"The BIOS did not expose BIOSAttributeInterface" on a machine that has it.**
  The probe queried only the modern CIM/WSMan provider (`Get-CimInstance`). Some
  Dell ACPI-WMI classes are reachable only over the legacy DCOM provider
  (`Get-WmiObject`) — which is exactly what Dell's own sample scripts use for
  `root\dcim\sysman\biosattributes`, and what community forks using CIM do not.
  Every read and write now tries CIM first and falls back to DCOM, recording the
  error from each attempt, and reports which provider answered.

### Added
- **`xps-helper diagnose-wmi`** — a deep WMI report: whether any ACPI-WMI
  (`PNP0C14`) devices exist at all, which namespaces and classes are present,
  each candidate interface probed through **both** providers with the exact
  exception text, and the current/possible values of the target attributes. The
  output is designed to be pasted into a bug report.
- **"Diagnose WMI" button** in the GUI, showing the same report without needing a
  terminal.
- `doctor` now points at `diagnose-wmi`, and distinguishes "interface missing"
  from "interface present but query denied" so the two don't require the same
  guesswork.

### Tests
266 tests (up from 244), including regression guards that the WMI scripts query
both providers and that PowerShell is launched with the console suppressed.

## v0.1.1

- Bumped CI actions to v7 (Node 24), removing Node 20 deprecation warnings.
- `doctor` distinguishes a permissions failure from a genuinely absent WMI-ACPI
  interface.

## v0.1.0

- Native Dell BIOS control over WMI-ACPI on Windows (`BIOSAttributeInterface`)
  and the kernel firmware-attributes class on Linux. No Dell software required.
- Dell Command | Configure supported as an optional fallback only.
- Thermal profiles, battery charge limits (including custom thresholds), battery
  status, temperatures, fan speeds, tray GUI, CLI, autostart, PyInstaller build.
