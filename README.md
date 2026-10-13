# XPS Helper

Thermal profile and battery charge control for Dell XPS laptops on Windows 11, in
the spirit of [G-Helper](https://github.com/seerge/g-helper).

Built for a **Dell XPS 13 7390 2-in-1**. **No Dell software is required** — no
Power Manager, no Command | Configure, no Command | Monitor.

```
$ xps-helper status
XPS Helper 0.2.0  --  XPS 13 7390 2-in-1
backends: control=wmi-bios, telemetry=wmi

Thermal profile : Quiet  (CPU power capped)
Charge          : Custom: charge from 60% up to 80%
Battery         : 76%, charging, health 91.3%
Temperature     : CPU Package 47.5 C
Profiles avail. : Optimized, Quiet, Cool, Ultra Performance
Charge modes    : Standard, Express Charge, Primarily AC Use, Adaptive, Custom
```

---

## How it works without Dell software

Dell's BIOS exposes a management interface over **ACPI-WMI** (the `PNP0C14`
device) on machines built from roughly 2018 onward. Dell's own name for it is
*Agentless Client Manageability* — "agentless" meaning the firmware provides it
and nothing needs to be installed.

On Windows it appears as ordinary WMI classes:

| Operation | WMI |
|---|---|
| Read settings | `root\dcim\sysman\biosattributes` → `EnumerationAttribute` (`AttributeName`, `CurrentValue`, `PossibleValues`) |
| Write settings | same namespace → `BIOSAttributeInterface` → `SetAttribute` |
| BIOS password state | `root\dcim\sysman\wmisecurity` → `PasswordObject` |

XPS Helper talks to that directly over PowerShell. WMI exposes the same class
through two stacks — modern CIM (`Get-CimInstance`) and legacy DCOM
(`Get-WmiObject`) — and **some machines only answer on one of them**, so the app
tries CIM first and falls back to DCOM, and tells you which one worked. The same interface is
available on Linux as `/sys/class/firmware-attributes/dell-wmi-sysman/`, which is
also implemented.

`cctk` (Dell Command | Configure) is still supported **as an optional fallback**
if you already happen to have it, but it is never required and never downloaded.

The attribute *values* are identical to cctk's, because they are the same BIOS
attributes: `Optimized` / `Quiet` / `Cool` / `UltraPerformance`, and
`Standard` / `Express` / `PrimAcUse` / `Adaptive` / `Custom:60-80`.

---

## Verification status — read this before trusting it

**Verified here:**

- **244 automated tests**, all passing: firmware token tables, Dell's threshold
  rules, WMI JSON interpretation, `PossibleValues` capability derivation, the
  `SetAttribute` status-code table (0=Success, 2=Invalid, 3=Access Denied,
  4=Not Supported), the Linux sysfs read/write path against a faked sysfs tree,
  persistence, write verification, and CLI exit codes.
- The full CLI end to end against the built-in mock backend.
- That a BIOS password never reaches the command line as a plain string literal.

**Not verified:** developed on a desktop that is not an XPS, so the Windows WMI
path has **never run against your actual firmware**. The interface contract comes
from Dell's documentation and Dell-authored scripts, not from your laptop.

So run this first, on the laptop:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\smoke_test_windows.ps1
xps-helper doctor
```

`-TestWrite` additionally proves write access by rewriting each attribute's
*current* value to itself, so nothing changes. If any assumption is wrong, both
commands print the raw BIOS output needed to fix it.

---

## Features

| G-Helper concept | XPS Helper | Notes |
|---|---|---|
| Performance profiles | ✅ Optimized / Quiet / Cool / Ultra Performance | Quiet and Cool cap sustained CPU power (~11 W), so they change performance, not just noise |
| Battery charge limit | ✅ Standard / Express Charge / Primarily AC Use / Adaptive / Custom | Custom takes a start/stop range, e.g. 60–80% |
| Battery health & status | ✅ | Percent, AC state, health %, time remaining |
| Temperature / fan monitoring | ✅ | CPU temp via LibreHardwareMonitor (open source) or ACPI |
| Tray icon with quick switching | ✅ | Icon colour reflects the active profile |
| CLI + scripting | ✅ | Every GUI action has a CLI equivalent |
| Fan curves / manual fan speed | ❌ | The fan is owned by the EC/BIOS on an XPS 13 |
| TDP sliders, GPU/MUX modes, RGB | ❌ | Not exposed on this hardware |

### Honest limitations

- **Manual fan control is not possible.** The fan is EC-controlled on an XPS 13.
- **TDP cannot be set directly.** Quiet/Cool is the supported way to cap power.
- **Undervolting is not offered.** Blocked by microcode on updated BIOSes.
- **If your BIOS lacks the WMI-ACPI interface**, XPS Helper still monitors the
  machine but cannot change settings. `doctor` will say so explicitly rather than
  failing cryptically.

---

## Requirements

- **Windows 11** (Windows 10 should work; untested).
- Either the **prebuilt .exe** (no Python needed — see Releases), or **Python 3.9+**.
- Administrator rights to *change* settings. Reading does not need them.

Optional, and **not Dell software**:

- **[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)** — accurate CPU package temperature and fan RPM. Open source.
- **Dell Command | Monitor** — optional extra sensor source. Not required.
- **pystray + Pillow** — tray icon. Without them the app runs as a normal window.

---

## Install

### Option A — prebuilt .exe (recommended)

Download `xps-helper-windows.zip` from the repository's **Releases** page and
unzip it. You get:

- `xps-helper.exe` — CLI
- `xps-helper-gui.exe` — tray GUI (no console window)

The executables are built by GitHub Actions on a Windows runner, so no Python is
needed on your machine.

### Option B — run from source

```powershell
pip install -e ".[gui]"
xps-helper doctor
```

Without installing:

```powershell
$env:PYTHONPATH = "src"
python -m xpshelper doctor
```

### Option C — build the .exe yourself

```powershell
pip install pyinstaller
pyinstaller packaging/xpshelper.spec --noconfirm
```

---

## Quick start

```powershell
# 1. What does this machine actually support?
xps-helper doctor

# 2. Current state.
xps-helper status

# 3. Performance profile.
xps-helper profile quiet

# 4. Cap charging at 80% to extend battery lifespan.
xps-helper charge primarily-ac

# ...or pick your own range.
xps-helper charge custom --start 60 --stop 80

# 5. Tray app.
xps-helper gui
```

Run as **Administrator** if you intend to change settings.

---

## CLI reference

```
xps-helper [--backend auto|native|cctk|mock|null] [--config PATH] [-v] <command>
```

| Command | Purpose |
|---|---|
| `status [--json] [--watch] [--interval N]` | Profile, charge setting, battery, temperature, fans |
| `profile [name] [--list] [--json] [--no-verify]` | Get or set the thermal profile |
| `charge [mode] [--start N] [--stop N] [--list] [--json]` | Get or set the charge configuration |
| `doctor [--json] [--no-raw]` | Diagnose which features work on this machine |
| `capabilities [--json]` | What the BIOS reports as supported |
| `diagnose-wmi [--json]` | Deep report: which Dell WMI interfaces exist, probed via both providers |
| `settings [...]` | View/change persisted settings |
| `apply` | Re-apply saved settings now |
| `gui [--no-tray]` | Launch the tray GUI |

Profile aliases: `quiet`/`silent`, `cool`, `optimized`/`balanced`,
`ultra-performance`/`performance`/`turbo`.
Charge aliases: `standard`, `express`, `primarily-ac`, `adaptive`, `custom`.

**Backends:** `native` = the firmware interface (default), `cctk` = Dell's CLI if
installed, `mock` = simulated XPS 7390 for demos on any machine, `null` = disable.

**Exit codes:** `0` success · `1` runtime failure · `2` usage error ·
`3` BIOS password required or rejected.

---

## GUI

```powershell
xps-helper gui
```

- Radio buttons apply immediately (like G-Helper).
- Options your firmware does not support are **disabled** based on a live probe,
  not hidden.
- Closing the window hides it to the tray; **Quit** in the tray menu exits.
- The tray icon changes colour with the active profile.
- **Doctor report** opens the full diagnostic text.

---

## Autostart

```powershell
# elevated PowerShell
.\packaging\install-autostart.ps1 -GuiPath .\dist\xps-helper\xps-helper-gui.exe

# also re-apply saved settings on unlock
.\packaging\install-autostart.ps1 -GuiPath .\dist\xps-helper\xps-helper-gui.exe `
                                  -CliPath .\dist\xps-helper\xps-helper.exe -ApplyOnUnlock

.\packaging\install-autostart.ps1 -Uninstall
```

A scheduled task is used instead of a Startup shortcut because writing BIOS
settings needs administrator rights, and a shortcut cannot elevate silently.

---

## BIOS passwords

If your BIOS has an administrator password, the firmware denies writes with
status `3` (Access Denied). XPS Helper reports that as "password required" and
offers two ways to supply it:

- **GUI** — the *BIOS password* field. Held in memory for the session, never
  written to disk.
- **CLI/scripts** — the `XPSHELPER_BIOS_PWD` environment variable.

The password is JSON-encoded, base64-wrapped, and decoded inside PowerShell, so
it never appears as a PowerShell string literal and cannot break out into the
command text. There is a test for that.

---

## Configuration

`%APPDATA%\xps-helper\config.json`:

```json
{
  "profile": "Quiet",
  "charge_mode": "Custom",
  "charge_start": 60,
  "charge_stop": 80,
  "refresh_seconds": 5,
  "apply_profile_on_startup": false,
  "reapply_on_power_change": false
}
```

Written atomically; a corrupt or hand-edited file falls back to defaults instead
of blocking startup.

---

## How it is built

```
              CLI (argparse)          GUI (tkinter + optional pystray)
                     \                     /
                      \                   /
                       XpsHelper service layer
                 (persistence, write verification, capability probe)
                              |
                    CompositeControlBackend
             (first backend that can actually control wins)
                    /                    \
        WmiBiosBackend              LinuxSysmanBackend
      (Windows, WMI-ACPI)        (Linux, firmware-attributes)
                    \                    /
                     CctkBackend (optional fallback)

        TelemetryBackend: WMI (Windows) | sysfs+psutil (Linux) | mock
```

Three decisions worth knowing:

1. **Capabilities are probed, never assumed.** The BIOS reports `PossibleValues`
   per attribute; the app reads that, so it shows what your machine can really
   do. If the text cannot be parsed it **fails open** — showing all documented
   values and saying why — because under-reporting would silently hide a working
   feature.

2. **A write is not successful until it is read back.** Dell's firmware declines
   some writes without returning an error (a profile change while a Thunderbolt
   dock is attached, for example). Reporting success there would leave you
   believing a setting changed when it did not.

3. **Monitoring never raises.** If one sensor fails, the rest of the status still
   displays, with a warning.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `doctor` says the BIOS did not expose `BIOSAttributeInterface` | Machine predates WMI-ACPI, the interface is disabled, or it is reachable only over the legacy provider | Run `xps-helper diagnose-wmi` (or the GUI's **Diagnose WMI** button) and send the report |
| A PowerShell window flashes up repeatedly | Fixed in v0.2.0 | Update to v0.2.0 or later |
| "Not elevated" warning | Not running as administrator | Writes need elevation; reads do not |
| "password required" / status 3 | BIOS administrator password is set | Enter it in the GUI, or set `XPSHELPER_BIOS_PWD` |
| "The firmware reported X after we asked for Y" | The write was silently declined | Detach a Thunderbolt dock and retry |
| No temperature shown | No sensor source | Run LibreHardwareMonitor, or rely on the ACPI fallback |
| No fan speed shown | No Dell Command | Monitor installed | Optional. Manual fan control is impossible regardless |
| Tray icon missing | pystray/Pillow not installed | `pip install -e ".[gui]"`, or just use the window |

Logs: `%APPDATA%\xps-helper\xps-helper.log`, and stderr with `-v`.

---

## Development

```bash
python -m pytest          # 244 tests, no hardware required
```

The suite runs against the mock backend, canned Dell output, and a faked sysfs
tree, so it works on any OS — including the Linux box this was developed on.

```
src/xpshelper/
  model.py          enums holding Dell's exact firmware tokens; threshold rules
  constants.py      WMI namespaces, attribute names, status codes, cctk paths
  service.py        persistence, write-with-verification, status assembly
  doctor.py         self-diagnosing capability report
  cli.py            argparse front end
  backends/
    wmi_bios.py     Windows: native WMI-ACPI BIOS attributes  <- primary
    linux_sysman.py Linux: kernel firmware-attributes sysfs   <- primary
    cctk.py         optional fallback via Dell Command | Configure
    composite.py    picks the first backend that can control
    wmi.py          telemetry: battery, temps, fans
    linux.py        telemetry: sysfs + psutil
    mock.py         simulated XPS 7390
  gui/              tkinter window, optional pystray tray
```

Everything hardware-specific sits behind the `ControlBackend` /
`TelemetryBackend` protocols, which is what makes it testable without a Dell.

---

## Credits and license

The Windows WMI interface details, the Linux sysfs layout, and the attribute
token values come from Dell's documentation and from open-source projects. See
[CREDITS.md](CREDITS.md).

**GPL-3.0-or-later** — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Dell. BIOS settings are changed through the
firmware's own documented interface. Use at your own risk.
