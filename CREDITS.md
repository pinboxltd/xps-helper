# Credits and attribution

## Third-party sensor providers

Package power and the PL1/PL2 power limits are read through interfaces published
by other projects, not reverse-engineered here:

- **LibreHardwareMonitor** and **OpenHardwareMonitor** -- their WMI ``Sensor``
  class, the same shape both projects have used for years.
- **HWiNFO** -- the "Report to Registry" layout under
  ``HKLM\SOFTWARE\HWiNFO64\VSB``.

None of these tools is bundled or required. They are read only when the user is
already running one. XPS Helper does not ship a kernel driver, because reading
MSRs directly would mean taking on a privilege surface much larger than this
tool's purpose.


## The DPTF temperature source

Unlike most of the interface knowledge above, this one was worked out on the
machine itself rather than taken from another project, so it is recorded here as a
finding rather than a debt.

Three sources were ruled out first: `MSAcpi_ThermalZoneTemperature` reports "Not
supported"; the SMBIOS type 28 probes exposed by `Win32_TemperatureProbe` describe
the sensors but carry no reading; and `DCIM_NumericSensor` needs Dell Command |
Monitor. Dell's own assemblies were then read: `SmbLib` parses SMBIOS
`TemperatureProbe` tables (type 28 enum = 28) and exposes `GetThermalInformation`
alongside the USTT commands, which confirmed Dell gets no live value from SMBIOS
either.

The live source is `root\wmi :: EsifDeviceInformation`, published by the
"Intel(R) Dynamic Tuning Processor Participant" device. Undocumented for
user-mode consumption, discovered by enumerating WMI classes that expose a
`Temperature` property and testing which ones return data. It requires elevation,
and its zones carry no participant names.


XPS Helper is an independent Python re-implementation. It is **not** a fork, and
no source code was copied verbatim. However, a large part of its value comes from
interface knowledge that other people worked out first and published. That debt is
recorded here.

## Why this file exists

Dell's firmware interface is undocumented in the places that matter. The literal
strings the BIOS accepts — `UltraPerformance`, `PrimAcUse`, `Custom:75-80` — and
the per-model way cctk advertises what it supports cannot be derived from Dell's
user-facing documentation. Guessing them produces an app that looks fine and
silently does nothing on real hardware. Several projects below did the
reverse-engineering and testing; this project reads their findings instead of
repeating the work.

## Projects whose findings are used

### [seerge/g-helper](https://github.com/seerge/g-helper) — GPL-3.0
Used for the **visual design**, ported in [`gui/theme.py`](src/xpshelper/gui/theme.py)
and [`gui/widgets.py`](src/xpshelper/gui/widgets.py):

- The colour table from `RForm.InitColors`: the dark surface, the primary and
  secondary button fills, borders and text, and the light-mode equivalents.
- The accent colours (`colorEco`, `colorStandard`, `colorTurbo`, `colorCustom`)
  and the convention of giving each performance mode one, so the active option is
  identifiable by colour alone.
- The flat-control look — 1px bordered flat buttons, group headers on a
  contrasting strip — and the `dark` / `light` / `windows` theme setting names.
- The `RButton` behaviours worth copying: a `Secondary` variant for inset rows and
  a `Borderless` variant for header rows.

Its originals are C#/WinForms and this project is Python/tkinter, so no code
carries over; what is shared is the token set and the control behaviour.

### [alexVinarskis/dell-powermanager](https://github.com/alexVinarskis/dell-powermanager) — GPL-3.0
The most important source. Used for:

- The exact `cctk` attribute names: `ThermalManagement`, `PrimaryBattChargeCfg`,
  `AdvBatteryChargeCfg`.
- The valid values for each: `Optimized` / `Quiet` / `Cool` / `UltraPerformance`,
  and `Standard` / `Express` / `PrimAcUse` / `Adaptive` / `Custom`.
- The `Custom:start-stop` syntax and its documented bounds (start 50–95,
  stop 55–100, gap ≥ 5).
- The `cctk -H --<Attribute>` capability convention: an `Arguments:` list in which
  `+` marks the values *this model* accepts. This is what makes the capability
  probe possible instead of guessing.
- The cctk exit-code table (`0`, `58`, `65`, `66`, `67`, `112`) used to tell
  "BIOS password required" apart from "not supported".
- Where Dell's installer puts `cctk.exe` on Windows and Linux.
- The `AdvBatteryChargeCfg` schedule syntax.

### [AaronKelley/DellFanManagement](https://github.com/AaronKelley/DellFanManagement) — GPL-3.0
Used for:

- The `root/dcim/sysman` → `DCIM_NumericSensor` WMI class for reading fan RPM
  (matched by `ElementName` starting with "fan", value in `CurrentReading`) and
  for temperature probes.
- The LibreHardwareMonitor approach for real CPU package temperature.
- Evidence about which Dell fan-control mechanisms exist. It also documents the
  SMI/BZH interfaces used on Alienware and G-series machines — **this project
  deliberately does not use them**, because they require loading a kernel driver,
  which is a poor trade for an XPS 13 whose fan is EC/BIOS-owned anyway.

### [the0cp/tcc-g15](https://github.com/the0cp/tcc-g15) — GPL-3.0
Used negatively, which was worth the time: its WMI documentation covers the
Alienware Command Center interface (`root\WMI` → `AWCCWmiMethodFunction`, thermal
modes as magic integers such as `152`/`171`). That established that the
Alienware/G-series path is a *different* interface from the one an XPS 13 uses, so
it was ruled out early rather than implemented and found not to work.

### Others consulted
- **Dell** — [Dell Command | Configure](https://www.dell.com/support/kbdoc/en-us/000178000/dell-command-configure)
  reference guide and the Dell Power Manager user guide (behaviour of the thermal
  modes and battery charge modes).
- **[ArchWiki: Dell XPS 13 (7390)](https://wiki.archlinux.org/title/Dell_XPS_13_(7390))**
  and the [9370 page](https://wiki.archlinux.org/title/Dell_XPS_13_(9370))** —
  the `platform_profile` thermal modes, the ~11 W CPU power cap under Quiet/Cool,
  and `cctk --PrimaryBattChargeCfg` usage on Linux.
- **[Debian bug #1087138](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1087138)**
  / [dell/libsmbios#147](https://github.com/dell/libsmbios/issues/147) — why
  `smbios-thermal-ctl` stopped working on kernel 6.11 and why
  `/sys/firmware/acpi/platform_profile` is now the supported path.
- **Linux kernel**: Armin Wolf's patch adding the XPS 13 7390 to the
  `dell-smm-hwmon` fan-control whitelist
  ([lkml](https://lkml.iu.edu/hypermail/linux/kernel/2206.1/04811.html)) —
  relevant to Linux users of this machine.

## The native WMI-ACPI path (added after the first release)

The original version drove Dell's `cctk.exe`, which meant the user had to
download Dell software. The native path that replaced it was assembled from:

### Linux kernel `dell-wmi-sysman` — GPL-2.0
`drivers/platform/x86/dell/dell-wmi-sysman/`. Used for:

- The WMI GUIDs: `F1DDEE52-063C-4784-A11E-8A06684B9BF4` (attribute interface),
  `...9BF5` (enumeration), `...9BFA` (integer), `...9BF9` (string),
  `0894B8D6-44A6-4719-97D7-6AD24108BFD4` (passobj), and the password interface.
- The method IDs on the attributes interface (`0x02` set defaults, `0x03` set
  BIOS defaults, `0x04` set attribute).
- The buffer layout for a write: a security area (`u32` security type, `u32`
  length, then the credential) followed by length-prefixed UTF-16 strings for the
  attribute name and value.

Written by Dell engineers and reviewed on LKML, which makes it the most
authoritative description of this interface that exists publicly.

### Dell — *Agentless Client Manageability* technical whitepaper
Establishes that this interface is provided by the firmware itself and requires
no installed agent. That single fact is what makes "no Dell software" possible.

### Dell-authored PowerShell samples — Apache-2.0
[gwblok/garytown](https://github.com/gwblok/garytown) (BIOS management scripts,
derived from Dell's own [svenriebedell/Intune](https://github.com/svenriebedell/Intune)
remediation scripts) and [ConfigJon/Firmware-Management](https://github.com/ConfigJon/Firmware-Management).
Used for the Windows-side names that the kernel driver does not need and
therefore does not contain:

- Namespace `root\dcim\sysman\biosattributes`, class `BIOSAttributeInterface`,
  method `SetAttribute`.
- The read path: `EnumerationAttribute` with `AttributeName` / `CurrentValue`.
- The security-handle arguments for a BIOS administrator password (`SecType = 1`,
  `SecHndCount`, `SecHandle` as UTF-8 bytes).
- The status-code table: 0 Success, 1 Failed, 2 Invalid Parameter,
  3 Access Denied, 4 Not Supported, 5 Memory Error, 6 Protocol Error.
- The `root\dcim\sysman\wmisecurity` → `PasswordObject` check for whether an
  admin password is set.

This project re-implements the logic in Python; the PowerShell samples are not
redistributed.

### [rhboot/efivar-test-data](https://github.com/rhboot/efivar-test-data)
Contains real sysfs dumps from Dell machines. The `xps13-9380` dump shows both
the legacy SMBIOS GUID (`A80593CE-A997-11DA-B012-B622A1EF5492`) and the
`F1DDEE52-…` sysman GUIDs present on XPS 13 hardware of that generation, while
older models (e.g. `xps13-9350`) show only the legacy one. That was the evidence
that a 2019-era XPS 13 is likely to expose the native attribute interface at all
— rather than something this project assumed.

### [fwts](https://github.com/ColinIanKing/fwts) — `src/acpi/wmi/wmi.c`
The `GUID → driver name` mapping table that confirmed
`F1DDEE52-…9BF4` belongs to `dell-wmi-sysman`.

### [ArchWiki: Laptop/Dell](https://wiki.archlinux.org/title/Laptop/Dell)
Documents the Linux equivalents (`smbios-thermal-ctl`, `smbios-battery-ctl` from
libsmbios, and the `platform_profile` ACPI interface) and notes that on some
devices cctk is the only way to persist custom battery charge behaviour — a
caveat this project now handles by using the firmware attributes directly.

## Licensing consequence

The sources above are GPL-3.0. Because this project builds on the interface
knowledge they established, **XPS Helper is licensed GPL-3.0-or-later** rather
than something more permissive. If you reuse this code, keep it that way.

## Third-party runtime components

XPS Helper bundles nothing. At runtime it may use:

- **Dell Command | Configure** (`cctk.exe`) — Dell's own tool, not redistributed
  here. Required for changing settings.
- **Dell Command | Monitor** — optional; provides fan speed and temperature
  sensors through WMI.
- **LibreHardwareMonitor** — optional; provides CPU package temperature.
- **pystray** and **Pillow** — optional Python packages, only for the tray icon.
