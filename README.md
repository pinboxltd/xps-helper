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

**Verified on real hardware.** The Windows WMI-ACPI path has been exercised
against a **Dell XPS 13 7390 2-in-1, BIOS 1.35.0**, which is the machine this
project targets. What that run established:

- The firmware does expose `root\dcim\sysman\biosattributes`, and it answers over
  the **CIM** provider.
- **92** enumeration attributes, **9** integer attributes and **2** string
  attributes are readable.
- **Battery charge control works end to end.** Writing `PrimaryBattChargeCfg` was
  confirmed by reading the value back after a real change, and the firmware
  reported these permitted values, exactly the tokens the app sends:
  `Adaptive | Standard | Express | PrimAcUse | Custom`.
- **Custom charge thresholds work**, but not the way cctk suggests: the combined
  `Custom:60-80` token is *rejected* (status 1, Failed) by this firmware. The
  bare `Custom` token plus the two integer attributes `CustomChargeStart` and
  `CustomChargeStop` all return status 0. The app does it that way.
- **`ThermalManagement` does not exist on this model.** Of the 92 enumeration
  attributes, none is a thermal profile. Thermal-profile switching is therefore
  **not available on an XPS 13 7390 2-in-1** — that is a firmware limitation, not
  a bug in this app. `doctor` says so explicitly.
- **Every write needs administrator rights**, even though reads do not. An
  unelevated process is refused with "Access denied" while the namespace itself
  is plainly present.
- No temperature or fan sensors were available on the test machine: it has no
  Dell Command | Monitor, no `MSAcpi_ThermalZoneTemperature`, and no
  LibreHardwareMonitor. Battery reporting worked; temperature did not.

**Off-hardware**, the suite is **325 automated tests** covering firmware token
tables, Dell's threshold rules, WMI JSON interpretation, `PossibleValue`
capability derivation, the `SetAttribute` status-code table (0=Success,
2=Invalid, 3=Access Denied, 4=Not Supported), the Linux sysfs read/write path
against a faked sysfs tree, persistence, write verification, elevation plumbing
and exit codes, and the GUI's capability-driven control states.

To re-check any of this on your own machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_hardware.ps1 -TestWrite
xps-helper doctor
```

`verify_hardware.ps1` prints the raw firmware values, so a wrong assumption in
the Python can be corrected from real output instead of guesswork. `-TestWrite`
additionally exercises the write path by rewriting each attribute's *current*
value to itself, so nothing changes.

---

## Administrator rights

Reading status never needs elevation. **Changing a BIOS setting always does.**
On the target machine the firmware refuses unelevated access outright, so the
app can raise itself rather than telling you to go and find an admin prompt:

```powershell
xps-helper --elevate charge custom --start 60 --stop 80
xps-helper --elevate doctor
```

Windows shows its normal UAC prompt; nothing is bypassed, and the command is
never run elevated silently. The GUI offers a **Restart as administrator**
button when it detects that the firmware is refusing access. If you dismiss the
prompt the app says so and reports that **nothing was changed**, rather than
claiming a write it cannot confirm.

---

## Dell's thermal-mode interface (why profiles need a second path)

On an XPS 13 7390 2-in-1 the BIOS attribute interface exposes 103 attributes and
**none of them is `ThermalManagement`** — so `xps-helper profile quiet` cannot
work through the ordinary path, and the raw write returns status 1. Yet Dell
Power Manager does change the thermal mode on this exact machine.

That is a *second, different interface*. It was recovered by decompiling Dell
Power Manager 3.9 (the IL reader is in [`tools/`](tools); the resulting analysis
lives in [`src/xpshelper/thermal.py`](src/xpshelper/thermal.py)). The call chain is:

```
ThermalManagementProvider.SetThermalMode(mode)
  -> SystemInterop.SetUsttInformation(UInt32 mode, Nullable<AAC> aac)
       -> InterfaceBufferSize   (WmiDescriptor.BufferLength)
       -> MakeBiosCall(buffer)
            -> DataAbstractor.ExecuteDACommand(buffer)
                 -> DAInterface.ExecuteDACommand
                      namespace root/wmi, class BDat
                      instance  ACPI\PNP0C14\0_0
                      set       Bytes = <interface buffer>
                      get       BFn
                      invoke    DoBFn()
                      read      Data -> back into the buffer
```

Three things worth knowing:

1. **It is not a raw SMI hack.** The neighbouring `ReadSwSmi` / `ReadCpuMmio32` /
   `ExecuteDACI` exports in `Dell.TechHub.NativeLibraryWrapper.dll` belong to
   Dell's diagnostics and DACI features. Thermal goes through an ordinary
   **ACPI-WMI method invocation**.
2. **USTT is Intel's mechanism, not a Dell invention.** *User Selectable Thermal
   Tables* is Intel DPTF's thermal-policy selector — the same thing Linux exposes
   as `/sys/bus/platform/devices/INT3400/current_uuid`.
3. **Capability is declared in SMBIOS**, and this machine declares it:

   ```
   SmbiosTableMiscellaneousBiosFlags.AcpiWmiSupported = True
   supported command classes: DA_CLASS_00, 01, 04, 08, 17
   ```

   Read with `xps-helper`'s own probe or `scripts/probe_ustt.ps1`.

### The exact interface, recovered

SmbLib names every part of the WMI contract, and the classes are queryable:

```
root\wmi ::  BFn          Active : Boolean, InstanceName : String
                          DoBFn(Data : BDat) -> Boolean
root\wmi ::  BDat         Bytes : UInt8Array        <- the DA command buffer
             instance     ACPI\PNP0C14\0_0
```

The constants SmbLib uses, recovered from its metadata:

```
WMI_SCOPE                        NAMESPACE_ROOT_WMI
WMI_CLASS_NAME_BDAT              WMI_CLASS_NAME_BFN
WMI_BFN_METHOD_DOBFN             WMI_ACPI_INSTANCE
WMI_PROPERTY_NAME_BYTES          WMI_PROPERTY_NAME_DATA / _QDATA
WMI_PROPERTY_NAME_INSTANCENAME   WMI_PROPERTY_NAME_SIZE / _SMBIOSDATA
WMI_QUERY_GET_ALL_RAW_SMBIOS_TABLES
```

Frame sizes and command IDs, from `SystemInterop`'s IL:

| Method | Constants | Meaning |
|---|---|---|
| `GetUsttInformation` | `256, …, 551026, 1, 256, 2` | 256-byte frame; command `0x000868B2` |
| `SetUsttInformation` | `…, 1, 4, 0, 1, …` | 32-bit mode value at **offset 4**, then `BlockCopy` |

Two traps worth recording, because both looked like "the feature does not exist":

- **`__CLASS` enumeration fails on ACPI-WMI classes** (`Invalid class`), so
  `Get-CimInstance __CLASS` reports `root\wmi` as empty even though `BDat` and
  `BFn` are present. They must be queried **by name**, which is what Dell does.
- **`BFn` returns 0 instances unelevated.** The interface is privilege-gated in
  the same way as the BIOS attributes, so an unelevated probe sees nothing.

### Why the helper needs exactly one prompt

Measured on the target machine:

| Registration | Unelevated result |
|---|---|
| plain task (`schtasks /create`) | succeeds |
| `RunLevel Limited` | succeeds |
| **`RunLevel Highest`** | **"Access is denied."** |

So the single install prompt is a Windows boundary, not a design choice — and it
is the *only* prompt. Everything afterwards runs through the registered task with
no UAC at all. There is no supported way to create a highest-privilege task
silently; that is the point of the boundary.

### Status — thermal modes work

Capability probing works and is read-only, and **setting a mode now works too**. The
interface buffer's layout is no longer a guess: it was recovered from Dell's own IL
and then confirmed against this machine's firmware byte for byte.

```powershell
xps-helper thermal            # read the current mode
xps-helper thermal Quiet      # set it
xps-helper thermal --json
```

On an XPS 13 7390 2-in-1 this reports `Balanced, Cool, Quiet, Ultra`, and a write is
confirmed by reading the value back out of firmware. It runs with **no UAC prompt**
through the privileged helper.

The recovered frame is a 44-byte `DA_CALL_STANDARD_INTERFACE`: `Class = 17`,
`Selector = 19`, `cbArg1` 0 for a read and 1 for a write, `cbArg2` the mode, and
`cbRes1` pre-seeded with `-3` as a status field the firmware clears on success. The
reply is decoded at offsets 20 (status), 24 (supported-mode bitmask), 28 (current
mode) and 31 (fan failure). `ThermalTableModes` is a `[Flags]` enum —
`Balanced = 1`, `Cool = 2`, `Quiet = 4`, `Ultra = 8`.

Dell Power Manager still works and remains a fine alternative; the difference is that
XPS Helper now does it too, and does it in its own code rather than by driving Dell's
binaries.

### Doing it without a UAC prompt

Worth recording, because it is the answer to "why does Dell never prompt?":
Dell's privileged work runs in **`Dell.CommandPowerManager.Service`**, a COM+
component that is *already running elevated*. Dell's unelevated UI delegates to it
over an authenticated pipe — its own code has a `UseComponentService` switch and
`VerifyComponentServiceAccess()` for exactly this. That service object
**instantiates unelevated**, which is why Dell shows no prompt.

XPS Helper does the same thing itself, without depending on Dell's software:

```powershell
xps-helper helper --install     # ONE UAC prompt, ever
xps-helper helper               # status: is it installed?
xps-helper helper --uninstall
```

There is also a general escape hatch, which is how the descriptor capture below was
done. Some things are simply unreadable without elevation — Dell's
`root\wmi :: WMI_Query` answers **"Access denied"** otherwise — and re-launching
elevated per probe would mean a prompt each time:

```powershell
xps-helper privileged-run -- powershell -NoProfile -File .\scripts\capture_descriptor.ps1
```

Measured at 2.4 s with no prompt.

`--install` registers a Scheduled Task (``RunLevel Highest``, **no trigger** — it
exists to be started, never to run on its own). After that, privileged actions are
triggered with `schtasks /run`, which needs **no prompt**: the elevation decision
was made once, at registration time. A Scheduled Task rather than a Startup
shortcut precisely because a shortcut cannot elevate silently.

**Verified on an XPS 13 7390 2-in-1**: four consecutive privileged charge writes
with no UAC prompt, each confirmed by reading the firmware back.

Two constraints worth knowing:

- **`RunLevel Highest` requires admin.** Plain tasks and `RunLevel Limited`
  register unelevated; `Highest` returns "Access is denied". So the single install
  prompt is a Windows security boundary, not a design choice — and there is no
  supported way around it.
- **The task must use `Interactive` logon, not `S4U`.** S4U does not load a user
  profile, so a virtualenv's `python.exe` cannot bootstrap and the task fails with
  "file not found" before Python runs. The installer therefore **self-tests the
  task and removes it** if the task cannot run its own command, because a helper
  that looks installed but cannot run is worse than none.

The privileged child takes its work from a per-user request file rather than a
command-line argument, because a task action is a fixed command line. That file is
validated on arrival and **refused if missing, malformed, empty, or older than
120 seconds**, and the child removes it once read. The request also sets an explicit
`XPSHELPER_PRIVILEGED_CHILD` marker: without it the child would decide it needed a
helper of its own and start another task, recursively.

Once installed, a charge change made in the GUI or CLI is routed through the helper
automatically, so it stops prompting. Routing is skipped when the process is already
privileged (detected by that marker, not by an elevation check, which can fail and
silently report "not elevated"), and when no helper is installed nothing changes.

---

## Features

| G-Helper concept | XPS Helper | Notes |
|---|---|---|
| Performance profiles | ⚠️ If the firmware exposes them | The app supports Optimized / Quiet / Cool / Ultra Performance, but **an XPS 13 7390 2-in-1 does not expose `ThermalManagement`**, so on that model the feature is unavailable and `doctor` says so. Other XPS models may expose it |
| Battery charge limit | ✅ Standard / Express Charge / Primarily AC Use / Adaptive / Custom | Verified writing and reading back on real hardware. Custom takes a start/stop range, e.g. 60–80% |
| Battery health & status | ✅ | Percent, AC state, health %, time remaining |
| Temperature / fan monitoring | ✅ | CPU temp via LibreHardwareMonitor (open source) or ACPI. **Neither was present on the test machine**, so this path is untested against real sensors |
| Tray icon with quick switching | ✅ | Icon colour reflects the active profile |
| CLI + scripting | ✅ | Every GUI action has a CLI equivalent, plus `--elevate` |
| Fan curves / manual fan speed | ❌ | The fan is owned by the EC/BIOS on an XPS 13 |
| TDP sliders, GPU/MUX modes, RGB | ❌ | Not exposed on this hardware |

### Honest limitations

- **Thermal profiles may not exist on your model.** Check with `xps-helper
  doctor` (or `capabilities`). On an XPS 13 7390 2-in-1 the BIOS exposes 92
  enumeration attributes and none of them is a thermal profile, so this feature
  cannot work there. The app reports that as a firmware limitation instead of
  failing cryptically.
- **Changing any setting needs administrator rights.** Use `--elevate`, or the
  GUI's *Restart as administrator* button.
- **Manual fan control is not possible.** The fan is EC-controlled on an XPS 13.
- **TDP cannot be set directly.** Quiet/Cool is the supported way to cap power,
  where the firmware offers it.
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

# 3. Performance profile (BIOS attribute; absent on some models).
xps-helper profile quiet

# 4. Thermal mode (Dell ACPI-WMI; works where the attribute above does not).
xps-helper thermal            # read
xps-helper thermal Quiet      # set

# 5. Cap charging at 80% to extend battery lifespan.
xps-helper charge primarily-ac

# ...or pick your own range.
xps-helper charge custom --start 60 --stop 80

# 6. Tray app.
xps-helper gui
```

Run as **Administrator** if you intend to change settings, or install the privileged
helper once (`xps-helper helper --install`) and nothing will prompt again.

---

## CLI reference

```
xps-helper [--backend auto|native|cctk|mock|null] [--config PATH] [--elevate] [-v] <command>
```

| Command | Purpose |
|---|---|
| `status [--json] [--watch] [--interval N]` | Profile, charge setting, battery, temperature, fans |
| `profile [name] [--list] [--json] [--no-verify]` | Get or set the thermal profile |
| `thermal [mode] [--json]` | Get or set the **Dell ACPI-WMI** thermal mode |
| `charge [mode] [--start N] [--stop N] [--list] [--json]` | Get or set the charge configuration |
| `doctor [--json] [--no-raw]` | Diagnose which features work on this machine |
| `capabilities [--json]` | What the BIOS reports as supported |
| `diagnose-wmi [--json]` | Deep report: which Dell WMI interfaces exist, probed via both providers |
| `helper [--install\|--uninstall\|--force] [--json]` | Manage the no-prompt privileged helper |
| `privileged-run -- <cmd>` | Run a command elevated, with no prompt |
| `settings [...]` | View/change persisted settings |
| `apply` | Re-apply saved settings now |
| `gui [--no-tray]` | Launch the tray GUI |

`--elevate` can go before or after the command and re-runs it with administrator
rights. It is the only way to change a setting on a machine where the firmware
gates writes behind elevation (which is the case on the XPS 13 7390 2-in-1).

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
  not hidden. If the BIOS has no such attribute at all (no thermal profile on an
  XPS 13 7390 2-in-1, say) the whole group is disabled and the hint says why,
  rather than offering a button that can only fail.
- If the firmware is refusing access because the app is not elevated, a **Restart
  as administrator** button appears.
- Closing the window hides it to the tray; **Quit** in the tray menu exits.
- The tray icon changes colour with the active profile.
- **Doctor report** opens the full diagnostic text.
- The first reading takes a few seconds: probing the BIOS means enumerating every
  attribute, which is the single most expensive thing the app does. Later
  refreshes reuse it and are fast.

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
| `doctor` says the BIOS interface is present but "Windows denied access to it" | The app is not elevated; the firmware refuses unelevated writes **and** attribute reads | Run `xps-helper --elevate <command>`, or press **Restart as administrator** in the GUI |
| `doctor` says the BIOS did not expose `BIOSAttributeInterface` **and** the namespace is missing | Machine predates WMI-ACPI, or the interface is disabled in BIOS setup | Run `xps-helper diagnose-wmi` (or the GUI's **Diagnose WMI** button) and open an issue with the report |
| `Thermal profiles: This BIOS does not list ThermalManagement` | This model has no thermal-profile attribute at all | Not fixable in software. Charge control is unaffected. Confirmed on an XPS 13 7390 2-in-1, BIOS 1.35.0 |
| Writing a charge mode changes nothing, or reports a failure | Older builds sent `Custom:60-80` as one token, which this firmware rejects | Fixed in v0.2.1: Custom writes `CustomChargeStart`/`CustomChargeStop` and then the bare `Custom` token |
| A PowerShell window flashes up repeatedly | Fixed in v0.2.0 | Update to v0.2.0 or later |
| "password required" / status 3 | BIOS administrator password is set | Enter it in the GUI, or set `XPSHELPER_BIOS_PWD` |
| "The firmware reported X after we asked for Y" | The write was silently declined | Detach a Thunderbolt dock and retry |
| No temperature shown | No sensor source on this machine | Run LibreHardwareMonitor, or install Dell Command \| Monitor. Neither was present on the test machine |
| No fan speed shown | No Dell Command \| Monitor installed | Optional. Manual fan control is impossible regardless |
| Tray icon missing | pystray/Pillow not installed | `pip install -e ".[gui]"`, or just use the window |

Logs: `%APPDATA%\xps-helper\xps-helper.log`, and stderr with `-v`.

---

## Development

```bash
python -m pytest          # 325 tests, no hardware required
```

The suite runs against the mock backend, canned Dell output, and a faked sysfs
tree, so it works on any OS — the canned payload shapes mirror what a real
XPS 13 7390 2-in-1 running BIOS 1.35.0 actually returns.

```
src/xpshelper/
  model.py          enums holding Dell's exact firmware tokens; threshold rules
  constants.py      WMI namespaces, attribute names, status codes, cctk paths
  service.py        persistence, write-with-verification, status assembly
  doctor.py         self-diagnosing capability report
  elevate.py        re-launching the app with administrator rights (Windows UAC)
  elevated_run.py   the entry point the elevated child runs (JSON request in,
                    stdout/stderr/exit-code files out)
  thermal.py        Dell's non-BIOS-attribute thermal interface (capability probe;
                    writes gated until the buffer layout is confirmed)
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

`scripts/verify_hardware.ps1` is the opposite of those tests: it inspects the
real firmware and prints the raw values, so an assumption baked into the Python
can be corrected from evidence.

---

## Credits and license

The Windows WMI interface details, the Linux sysfs layout, and the attribute
token values come from Dell's documentation and from open-source projects. See
[CREDITS.md](CREDITS.md).

**GPL-3.0-or-later** — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Dell. BIOS settings are changed through the
firmware's own documented interface. Use at your own risk.
