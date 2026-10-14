# Changelog

## v0.3.0

Thermal-mode control, reverse-engineered from Dell and replicated in this
project's own code, plus a redrawn window. This is the first release where the
performance mode can actually be changed on a model whose BIOS exposes no
`ThermalManagement` attribute — an XPS 13 7390 2-in-1 among them.

### Fixed a bug that only appeared in the packaged build

The thermal helper child was started as ``[sys.executable, "-c", snippet]``. That
works from a source checkout but not once packaged: in a frozen build
``sys.executable`` is ``xps-helper.exe``, not Python, so ``-c`` means nothing and
the snippet was parsed as a subcommand name:

```
xps-helper: error: argument command: invalid choice: "import json, sys; ..."
```

Thermal control therefore worked from source and was broken in the shipped exe —
the build nobody tests by hand. The child is now a real hidden subcommand,
``internal-thermal``, which is identical from source and from the bundle, and
``privileged.self_invocation()`` builds that argv in one place so no caller can
reintroduce the ``-c`` form.

### The window redrawn in G-Helper's style

The GUI now uses the visual language of
[G-Helper](https://github.com/seerge/g-helper) — the same palette, the same flat
controls, and the same idea that **the active option owns its accent colour**, so
the window reads at a glance. Both projects are GPL-3.0, so the design is shared
legitimately; `CREDITS.md` records the debt.

What was taken is the *token set and control behaviour*, not code. G-Helper's
originals are C#/WinForms (`RForm`, `ControlHelper`, `RButton`) and this is
Python/tkinter, so nothing transfers literally:

- **Palette** — `RForm.InitColors`'s dark surface (`#1c1c1c`), button fills
  (`#2e2e2e` / `#242424`), borders and text colours, plus the light half.
- **Accents** — its `colorEco` green, `colorStandard` blue, `colorTurbo` red and
  `colorCustom` orange, mapped onto Dell's modes as a temperature ladder: green
  `Quiet`, cyan `Cool`, blue `Balanced`, red `Ultra`.
- **Flat controls** — buttons are flat with a 1px border and never beveled, group
  headings sit on a contrasting strip, hints are subdued.
- **Theme modes** — `dark`, `light`, or `windows` to follow the OS, the same
  setting names G-Helper uses. Default follows the OS.

Two implementation notes worth keeping:

- **`ttk` cannot do this.** ttk styling is bound to the platform theme, so "flat
  dark button with a coloured active state" is not expressible. The mode buttons
  are plain `tk` widgets drawing on a `Canvas`.
- **Text contrast is computed, not assumed.** G-Helper hardcodes white on an
  active button, which would put white on the light custom orange. The active
  foreground is chosen from the accent's luma instead.

The theme is read once at construction rather than switched live: restyling every
widget on the fly is what makes this kind of GUI fragile.

### Thermal modes work — reverse-engineered and replicated

**This is the feature the whole investigation was for.** XPS Helper can now read and
set the performance mode on this XPS 13 7390 2-in-1, through an interface the BIOS
attribute path never had.

```
$ xps-helper thermal
Current thermal mode : Balanced
Available            : Balanced, Cool, Quiet, Ultra

$ xps-helper thermal Quiet
Thermal mode applied: Quiet.

$ xps-helper thermal Ultra
Thermal mode applied: Ultra.
```

Verified by reading the firmware back after each write, and it runs with **no UAC
prompt** through the privileged helper.

#### The recovered frame

`SmbLib` defines `DA_CALL_STANDARD_INTERFACE` as a 44-byte
`LayoutKind.Sequential` struct. `SystemInterop.GetUsttInformation` and
`SetUsttInformation` fill it **identically apart from `cbArg1`**:

| offset | size | field | read | write |
|---|---|---|---|---|
| 0 | u16 | `Class` | 17 (`0x11`) | 17 |
| 2 | u16 | `Selector` | 19 (`0x13`) | 19 |
| 4 | u32 | `cbArg1` | **0** | **1** |
| 8 | u32 | `cbArg2` | 0 | **the mode** |
| 12 | u32 | `cbArg3`, `cbArg4` | 0 | 0 |
| 20 | u32 | `cbRes1` | `0xFFFFFFFD` | `0xFFFFFFFD` |
| 24 | u32 | `cbRes2..cbRes4`, `ArgAttrib` | 0 | 0 |
| 40 | u32 | `BLength` | 0 | 0 |

The struct is `BlockCopy`'d into a `byte[32768]` buffer (the descriptor's
`BufferLength`) and handed to `DataAbstractor.ExecuteDACommand(WmiDescriptor, Byte[]&)`.

#### The recovered response

`AcpiWmi.ThermalTables.Parse` decodes the reply at these offsets:

| offset | field | rule |
|---|---|---|
| 20 | `cbRes1` → `Error` | must be 0 |
| 24 | `cbRes2` byte 0 → `SupportedThermalModes` | bitmask, valid 0…15 |
| 28 | `cbRes3` byte 0 → `CurrentThermalMode` | valid 0…8 |
| 31 | `FanFailureMode` | — |

`ThermalTableModes` is a `[Flags]` enum: `Balanced = 1`, `Cool = 2`, `Quiet = 4`,
`Ultra = 8`. This machine reports `SupportedThermalModes = 0x0f` and
`CurrentThermalMode = 1` → **Balanced, with all four modes available**.

#### Three traps that cost the most time

- **`cbRes1` is a status output, not an input.** Dell pre-seeds `-3` and the firmware
  overwrites it with `0` on success. Dell's `ErrorEnum` maps `-3` to "unsupported",
  which makes it a usable oracle. Reading it as an input makes a working command look
  like a rejected one.
- **A write reply must not be decoded with the read layout.** A write response does
  not carry the supported/current fields, so parsing it that way reports a current
  mode of `0`. Confirmation has to come from a fresh read — which is also the only
  thing that actually proves the firmware took the value. This bug shipped into the
  first version of `set_thermal_mode` and reported *"wrote 4, firmware reports 0"*
  for a write that had in fact succeeded.
- **`BDat` cannot be `Put()`.** It has no key (*"Attempt to put an instance with no
  defined key"*); Dell passes it as a transient method parameter.
- **PowerShell cannot pass `Byte[]&`.** The binder wraps the array and throws
  *"Object of type 'System.Object[]' cannot be converted to type 'System.Byte[]&'"*,
  so the by-ref buffer had to be driven from C#.

#### Why the low-level attempt failed for so long

Calling `root\wmi :: BFn.DoBFn` directly only ever **echoed the header back**.
`MakeBiosCall` does not call WMI itself — it routes through
`DataAbstractor.ExecuteDACommand`, which reads the reply with `GetOutputBuffer`'s
**DSCI framing** (`[u32 length][payload][DSCI]…`). Bypassing that wrapper meant
talking to the transport instead of the data-access layer.

### Added

- `xps-helper thermal [MODE]` — read or set the performance mode. `--json` for
  machine-readable output.
- A **Thermal mode (Dell ACPI-WMI)** row of radio buttons in the GUI, separate from
  the BIOS-attribute Performance radios, shown because the two are different
  interfaces. Modes the firmware does not offer are disabled, and the current mode is
  selected from the firmware report rather than assumed.
- `xps-helper privileged-run -- <command>` — run any command through the privileged
  helper with no UAC prompt. This is what made the descriptor capture possible:
  `root\wmi :: WMI_Query` answers "Access denied" unelevated.
- `tools/IlDisasm.cs` — a full IL disassembler built on `System.Reflection.Emit`'s
  opcode table, with operand decoding, call-target and string resolution. The earlier
  constants-only reader could not answer *which opcode consumes which constant*,
  which is exactly what the frame reconstruction needed.
- `tools/da_driver`, `tools/validate_ustt` — C# drivers that exercise Dell's real
  `ExecuteDACommand` path and report the decoded tables.
- `tools/da_driver` revealed the frame is constructed with
  `stfld DA_CALL_STANDARD_INTERFACE::Class` and friends, not by writing raw offsets.

### Tests

**402 tests** (from 382), including byte-exact pins of the hardware-captured frame,
the response decode against the values read from this machine, mode-name resolution,
and GUI radio states for firmware reports that offer fewer modes than this one does.

## v0.2.1

### The real command path: `DataAbstractor.ExecuteDACommand`

`MakeBiosCall` does **not** call WMI directly. Recovered from its IL:

```
IsClassSupported(buffer[0])                       // re-read from the frame
if UseComponentService:  new ComponentService().ExecuteDACommand(ref buffer)
else:                    DataAbstractor.ExecuteDACommand(descriptor, ref buffer)
```

`ComponentService` is a COM+ `ServicedComponent` gated on `_clientAuthenticated`,
so the usable route is `DataAbstractor`. Its signatures, read by reflection:

```
DataAbstractor()                                     constructible
WmiDescriptor Initialize()                           DELL v1 buffer=32768
Boolean ExecuteDACommand(WmiDescriptor, Byte[]&)     the real path
Byte[]  GetOutputBuffer(Int32 bufferSize)            DSCI-framed payload extractor
Boolean IsClassSupported(Int32)
```

**The response format is DSCI-framed.** `GetOutputBuffer` builds
`[u32 length][payload][DSCI][payload][DSCI]…` and returns null when no `DSCI`
marker is present. That is why calling `root\wmi :: BFn.DoBFn` directly only ever
echoed: it bypasses this wrapper entirely.

Driving `ExecuteDACommand` with the recovered frame — `tools/validate_ustt/DaDriver.cs`
— now returns real data where the low-level call returned zeros:

```
returned : True   cbRes1 = 0x00000000          (accepted)
0000  11 00 13 00 ...                          Class=17 Selector=19
0018  0f 00 00 00 01                           cbRes2 = 15, cbRes3 = 1
```

`cbRes2 = 15` is the supported-DA-class count (independently measured earlier), and
it was zero on the direct `DoBFn` path. So the wrapper is genuinely executing.

**cbRes1 is a status output, not an input.** Dell pre-seeds `-3`; the firmware
overwrites offset 20 with `0` on success. Dell's `ErrorEnum` maps `-3` to
"unsupported", which makes it a usable **oracle**. Enumerating with it:

```
Class=17 accepted selectors : 3, 11, 19, 22
Class=10, Selector=19       : accepted
```

Remaining unknown: no `DSCI` payload comes back for the read, so the read command
still does not yield the thermal tables. Two obstacles were removed (the wrapper and
the framing); the command semantics are the last piece.

### PowerShell cannot pass `Byte[]&`

Invoking a by-ref array parameter through PowerShell reflection fails with
*"Object of type 'System.Object[]' cannot be converted to type 'System.Byte[]&'"* —
the binder wraps the array. `DaDriver.cs` therefore does it in C# with an explicit
`object?[]` and reads the mutated array back, which works first time.

### The USTT command frame: recovered (superseded detail)

The frame struct is fully recovered from SmbLib/SystemInterop IL —
`DA_CALL_STANDARD_INTERFACE`, 44 bytes, `LayoutKind.Sequential`:

| offset | size | field | value in Dell's read path |
|---|---|---|---|
| 0 | u16 | `Class` | 17 (`0x11`) |
| 2 | u16 | `Selector` | 19 (`0x13`) |
| 4 | u32 | `cbArg1` | 0 = read, 1 = write |
| 8 | u32 | `cbArg2` | the mode value |
| 12 | u32 | `cbArg3`, `cbArg4` | 0 |
| 20 | u32 | `cbRes1` | `0xFFFFFFFD` (−3) |
| 24 | u32 | `cbRes2..cbRes4`, `ArgAttrib` | 0 |
| 40 | u32 | `BLength` | 0 |

`GetUsttInformation` and `SetUsttInformation` are byte-for-byte identical apart from
`cbArg1` (0 vs 1) and `cbArg2`. The struct is `BlockCopy`'d into a
`byte[InterfaceBufferSize]` (32768) and handed to `MakeBiosCall`.

`tools/validate_ustt` builds that frame and calls `root\wmi :: BFn.DoBFn`
natively, so **the transport is proven end-to-end from our own code**:

```
header sent : 11 00 13 00 00000000 00000000 ... fd ff ff ff ...      (44 bytes)
DoBFn       : invoked successfully on ACPI\PNP0C14\0_0, returns Data.Bytes[32768]
```

**But the firmware does not act on the command.** It merely echoes the header back
and reports the request header values in the response:

- `cbRes1 = 0` → response `0:11 2:13` (2 non-zero bytes)
- `cbRes1 = 1` → `0:11 2:13 20:01`
- `cbRes1 = 0xFFFFFFFF` → `0:11 2:13 20:ff 21:ff 22:ff 23:ff`

So every field is being echoed, and no thermal data is returned. `BLength` is
validated (32768 makes the firmware reject the command outright), but varying it
does not produce a payload either.

Conclusion: the header is right but the **command encoding is not**. Reproducing it
needs the actual frame Dell sends, not further inference. Two capture attempts
failed for structural reasons worth recording:

- **Polling `BDat.Bytes` cannot work.** `BDat` has no persistent instances — it is
  a transient per-call object Dell creates and discards, so the buffer never exists
  outside a live `DoBFn`. A 90 s poll at 5 ms intervals collected 0 samples.
- **ETW records that the call happened but not its arguments.** The
  `Microsoft-Windows-WMI-Activity` trace below proves `BFn.DoBFn` is invoked and
  with which instance, but carries no method parameters.

What would settle it: a WMI method-argument interceptor (an `IWbemServices` proxy),
or a debugger breakpoint in `DAInterface.ExecuteDACommand` on the live buffer.

### Runtime proof of the transport (ETW)

Static analysis predicted the WMI interface; a live ETW capture of
`Microsoft-Windows-WMI-Activity` during a real thermal-mode change in Dell Power
Manager **confirms it exactly**:

```
25 ×  Start IWbemServices::ExecMethod - root\wmi :
        BFn.InstanceName="ACPI\PNP0C14\0_0"::DoBFn
46 ×  Start IWbemServices::ExecQuery - root\wmi : select * from WMI_Query
47 ×  Provider::CreateInstanceEnum - WMIProv : WMI_Query
21 ×  SELECT * FROM MSSmBios_RawSMBiosTables
```

So the call really is `root\wmi :: BFn.DoBFn` on instance `ACPI\PNP0C14\0_0`, with
`WMI_Query` supplying the descriptor — precisely the chain recovered from the CIL.
`scripts/trace_wmi_activity.ps1` starts and stops the trace;
`scripts/read_wmi_trace.ps1` decodes it.

One trap worth recording: `Microsoft-Windows-WMI-Activity` writes its payload under
`<UserData>` in a **typed element whose name varies by record kind**
(`Operation_New`, `Operation_Start`, …), not under `<EventData>`. A parser reading
only `EventData` reports zero operations and makes it look as though the trace
captured nothing.

### Ghidra installed and verified

REA's native and process providers were unavailable because no analysis engine was
present. Ghidra 12.1.4 with Temurin JDK 21 is now installed, and `rea doctor`
reports **all nine Ghidra checks healthy**, including the native decompiler and
headless launcher:

```
ghidra                    : healthy
ghidra-version            : 12.1.4
ghidra-headless           : ...support/analyzeHeadless.bat
ghidra-native_decompiler  : decompile.exe reports pe x86_64
ghidra-java               : 21.0.12.1; 64-bit; JDK
ghidra-native_authority   : Verified Windows native Job Object, private DACL
```

`JAVA_HOME` and `GHIDRA_INSTALL_DIR` are persisted at user scope. A headless
analysis of `DellInstrumentation.sys` completes (x86_64, 10 segments).

### Dell's WMI descriptor, fully recovered

Using [REA](https://github.com/morluto/rea) for canonical metadata, plus a focused
IL reader, the whole ACPI-WMI descriptor was recovered. `WmiDescriptor::Initialize`
in `SmbLib.dll` queries `root\wmi :: WMI_Query` for `InstanceName
ACPI\PNP0C14\0_0` and reads its `QDATA` and `Bytes`. The captured descriptor:

```
QDATA.Bytes[128] = 44 45 4c 4c 20 57 4d 49 | 01 00 00 00 | 00 80 00 00 | 01 00 00 00 ...
                   "D E L L   W M I"       version=1     0x8000=32768   function count=1

Dell's own parser:  Vendor=DELL  Signature=" WMI"  Version=1
                    BufferLength=32768  IsInitialized=True
```

So the interface buffer is **32768 bytes**, and the header is a 32-byte structure
(`"DELL WMI"`, version, buffer length, function count) followed by per-function
entries. That is the container the USTT command travels in.

**REA's native-boundary analysis settled an open architectural question:**

| Assembly | P/Invoke | Native modules |
|---|---|---|
| `SmbLib.dll` | **0** | **0** |
| `SystemInterop.dll` | 47 | setupapi, kernel32, powrprof, user32 |

`SmbLib` is pure managed code reaching WMI through `System.Management`, and
`SystemInterop`'s native surface is device enumeration, power-scheme and
`DeviceIoControl` APIs — **the thermal functions are absent from it**. Combined,
this proves the thermal transport is **ordinary managed WMI with no native
barrier**. The `ReadSwSmi` / `ReadCpuMmio32` exports belong to other Dell features,
as previously established.

### Added — `xps-helper privileged-run`

Runs any command through the installed privileged helper, so an
elevation-requiring diagnostic costs no UAC prompt:

```powershell
xps-helper privileged-run -- powershell -NoProfile -File .\scripts\capture_descriptor.ps1
```

Measured at 2.4 s with no prompt. This is what made the descriptor capture above
possible: `root\wmi :: WMI_Query` returns **"Access denied"** unelevated.

### What still blocks the thermal write

`SystemInterop.GetUsttInformation()` returns an all-default
`ThermalTables` (`IsFeatureSupported = False`, `CurrentThermalMode = None`) even
with an initialised descriptor, because `SystemInterop` reads its descriptor from
its own internal `DataAbstractor` rather than from one supplied by a caller.
Reproducing Dell's full object graph needs **runtime** evidence, which static
analysis cannot provide — REA states the same limitation in its own output: *"did
not load the assembly, resolve dependencies through a CLR, decompile C#, or execute
target code."* REA's `capture-process` would fit, but `rea doctor` reports no
analysis engine available (Hopper, Ghidra and IDA are all absent), and that
command targets native/process work rather than managed object-graph construction.

The write path therefore still fails closed rather than guessing.

### Tests

**382 tests**, including guards for the privileged-run passthrough.


### Added — Dell's second thermal interface, reverse-engineered

An XPS 13 7390 2-in-1 exposes **103 BIOS attributes and none is
`ThermalManagement`**, so the WMI-ACPI BIOS-attribute path cannot change the
thermal profile — yet Dell Power Manager can. Decompiling Dell Power Manager 3.9
established that this is a **separate interface**:

```
ThermalManagementProvider.SetThermalMode(mode)
  -> SystemInterop.SetUsttInformation(UInt32, Nullable<AAC>)
       -> MakeBiosCall(buffer)
            -> DAInterface.ExecuteDACommand
                 root/wmi :: BDat, instance ACPI\PNP0C14\0_0
                 set Bytes, get BFn, invoke DoBFn(), read Data
```

- `src/xpshelper/thermal.py` implements the **capability probe** (read-only) for
  this interface and records the transport constants.
- `tools/` carries the .NET IL reader used for the analysis, so the finding is
  reproducible rather than folklore. It decodes operand constants as well as call
  targets, which is what exposed the frame sizes and command IDs below.
- `scripts/probe_ustt.ps1`, `scripts/probe_dell_com.ps1`,
  `scripts/capture_ustt_buffer.ps1`, `scripts/dump_da_classes.ps1` and
  `scripts/check_thermal_support.ps1` capture the supporting experiments.

Frame details recovered by decompiling `SystemInterop`:

| Method | Constants (IL order) | Meaning |
|---|---|---|
| `GetUsttInformation` | `256, 0, 17, 19, -3, 0, 0, 0, 551026, 1, 256, 2` | 256-byte frame; command id `551026` = `0x000868B2` |
| `SetUsttInformation` | `0, 0, 17, 19, -3, 1, 4, 0, 1, 0, 0, 0, 0, 1` | 32-bit mode value written at **offset 4**, then `BlockCopy` into the frame |

The DA command selector `0xB2` (178) is visible in every SMBIOS `CallingInterface`
table (as `<class>/00B2`) and again in `BIOSIntf.dll` as `Type 0xB2 (178)`, which
ties the transport, the frame and the SMBIOS capability data together. The
`CDAToken` class in `BIOSIntf.dll` confirms the protocol model is
(class, selector, value).

The WMI contract is named explicitly in SmbLib's metadata, and the classes are
queryable by name:

```
root\wmi :: BFn    Active : Boolean, InstanceName : String
                   DoBFn(Data : BDat) -> Boolean
root\wmi :: BDat   Bytes : UInt8Array          <- the DA command buffer
```

Two traps found the hard way, both of which had been reporting the interface as
absent:

- **`__CLASS` enumeration fails on ACPI-WMI classes** (`Invalid class`), so
  `Get-CimInstance __CLASS` reports `root\wmi` as empty even though `BDat` and
  `BFn` are present. They must be queried **by name**, which is what Dell does.
  An earlier probe concluded "no Dell WMI classes exist" from this and was wrong.
- **`BFn` returns 0 instances unelevated** — the interface is privilege-gated just
  like the BIOS attributes, so an unelevated probe sees nothing.

Dell's protocol enums were then recovered from the same assembly, which replaces
guesswork with the actual constants:

| Enum | Values that matter |
|---|---|
| `CallingInterfaceClassValueEnum` | `DA_CLASS_00/01/04/08/17` — the classes the BIOS advertises |
| `SmbiosTableTypeEnum` | `CallingInterface = 218 (0xDA)`, `MiscellaneousBiosFlags = 177 (0xB1)` |
| `SmbiosTableHandleEnum` | `CallingInterface = 55808 (0xDA00)`, `MiscellaneousBiosFlags = 45312 (0xB100)` |
| `BatteryClassSelectEnum` | `SetDesktopBatteryMode = 16`, `GetDesktopBatteryMode = 17`, `GetBatteryInfoEx = 18`, `SetBatteryInfoEx = 19` |
| `ErrorEnum` | `0xFC` invalid interface buffer, `0xFA` too small, `0xFB` format error, `0xFF` interface error |

`0xDA00` explains an earlier mystery: the SMBIOS record marker `da fb` is the
CallingInterface table type in little-endian order, and the tables are emitted in
handle order — which is why a naive SMBIOS walk desynchronised and my first
hand-written parser produced confidently wrong results.

Two corrections to earlier assumptions, both evidence-based:

- Thermal is **not** a raw SMI/MMIO hack. The `ReadSwSmi` / `ReadCpuMmio32` /
  `ExecuteDACI` exports in `Dell.TechHub.NativeLibraryWrapper.dll` belong to
  Dell's diagnostics and DACI features. Thermal is an ordinary ACPI-WMI method
  call, and USTT is Intel DPTF's *User Selectable Thermal Tables* — the same
  mechanism Linux exposes as `INT3400/current_uuid`.
- Capability is declared in SMBIOS, and this machine declares it:
  `SmbiosTableMiscellaneousBiosFlags.AcpiWmiSupported = True`, with command
  classes `DA_CLASS_00/01/04/08/17`. An earlier read of `False` was an unparsed
  default object, not a real answer.

### Not enabled, deliberately

**Setting** a thermal mode is gated behind `USTT_LAYOUT_CONFIRMED`, because the
interface buffer's internal layout has not been confirmed on hardware. A wrong
byte pattern there goes into a firmware thermal controller and cannot be undone by
re-running the tool, so the write path fails closed with an explanation instead of
guessing. `read_thermal_mode()` refuses for the same reason rather than returning
a fabricated value. Dell Power Manager remains the supported route for thermal
profiles today.

### UAC, answered — and verified on hardware

Dell's privileged work runs in `Dell.CommandPowerManager.Service`, a COM+
component already running elevated; Dell's unelevated UI delegates to it over an
authenticated pipe (its own `UseComponentService` / `VerifyComponentServiceAccess`
path). That service object **instantiates unelevated**, which is why Dell never
prompts.

`xps-helper helper --install` implements the same shape without depending on
Dell's software: it registers a Scheduled Task at `RunLevel Highest` with **no
trigger**, and privileged actions are then started with `schtasks /run`, which
needs no prompt because the elevation decision was made once at registration.
`xps-helper helper` reports status and `--uninstall` removes it.

**Verified on the target XPS 13 7390 2-in-1** — four consecutive privileged writes
with no UAC prompt, each confirmed by reading the firmware back:

| Write | Result |
|---|---|
| Primarily AC Use | exit 0, 15.8 s, read back correctly |
| Custom 50–90 | exit 0, 15.8 s, read back correctly |
| Adaptive | exit 0, 15.8 s, read back correctly |
| Custom 50–90 (restored) | exit 0, read back correctly |

Measured, the single install prompt is a Windows boundary rather than a design
choice:

| Registration | Unelevated result |
|---|---|
| plain task (`schtasks /create`) | succeeds |
| `RunLevel Limited` | succeeds |
| **`RunLevel Highest`** | **"Access is denied."** |

There is no supported way to create a highest-privilege task silently; that is the
point of the boundary, and it is the only prompt.

### Four defects found by finally running it end to end

Each of these had been invisible to inspection and only appeared once the helper
was actually exercised:

- **Infinite recursion through the helper.** The privileged child ran the same
  high-level command, concluded it needed a privileged helper, and started another
  task — which started another child. The symptom was six hung processes and a
  180 s timeout with no useful error. Fixed with an explicit `XPSHELPER_
  PRIVILEGED_CHILD` marker, because relying on an elevation *check* is fragile: a
  failed token query reads as "not elevated" and lets the recursion through. A test
  deliberately makes `is_elevated()` lie to prove the marker alone stops it.
- **`S4U` logon cannot run the helper at all.** S4U does not load a user profile,
  so a venv's `python.exe` cannot bootstrap and the task fails with "file not
  found" (Last Result 2) before Python ever runs. Now uses `Interactive` logon, and
  the install **self-tests the task and unregisters itself** if the task cannot run
  its own command — registering a broken helper is worse than none, because it
  looks installed.
- **Validation ran before delegation.** The capability probe reads the firmware, and
  an unelevated process is refused ("this BIOS exposes no battery charge
  configuration attribute"), so the write aborted *before* reaching the helper that
  exists to do it elevated. Delegation now comes first.
- **The install script failed silently.** `$ErrorActionPreference = 'Stop'` with no
  `try/catch` meant a throwing cmdlet aborted it, and the user saw only
  `installed: False`. It is now wrapped, reports the principal and logon type, and
  the CLI reports the reason instead of a bare boolean.

### Test isolation

`tests/conftest.py` now disables the privileged helper by default. Installing it on
a developer machine made charge writes route through the task, silently bypassing
the in-process validation and firmware error paths several tests exist to check —
so four tests began failing for an environmental reason. Tests that exercise the
helper re-patch it themselves.

### Tests

**379 tests** (up from 325), including guards that the transport constants match
Dell's DA interface, that the capability probe is read-only, that the thermal write
path refuses rather than guessing, that the privileged request file fails closed on
missing/malformed/empty/stale input, that the helper task can never gain a trigger,
that the privileged child can never start another helper, that the install uses
Interactive logon and self-tests, and an end-to-end simulation of the helper
protocol.


First release validated against the real target machine — a **Dell XPS 13 7390
2-in-1, BIOS 1.35.0** — rather than only against canned output. That run found
three defects which between them meant **no BIOS setting could be changed on
that laptop at all**, plus a fourth that wedged the GUI.

### Fixed

- **Every write failed, and the failure was invisible.** `SetAttribute` declares
  `SecHandle`/`SecHndCount`/`SecType` *before* the outgoing `Status` parameter.
  The app omitted them when no BIOS password was set, and this firmware answers
  a call that omits `SecHandle` with `"Invalid parameter"` — an exception raised
  before any status code is returned. Because the exception was swallowed into
  the same field as a BIOS status, every write looked like a silent refusal.
  `SecHandle` is now always sent, zero-length when there is no password. Verified
  on hardware: `PrimaryBattChargeCfg` writes return `Status 0 (Success)` and read
  back correctly.

- **Custom charge thresholds did nothing.** Writing the combined cctk-style token
  `Custom:60-80` to `PrimaryBattChargeCfg` returns `Status 1 (Failed)` on this
  firmware and the mode does not change. What works is the bare `Custom` token
  plus the two integer attributes `CustomChargeStart` and `CustomChargeStop`.
  The backend now writes thresholds first and the mode last, so a partial failure
  cannot leave the machine in Custom mode with stale thresholds. Reading a Custom
  configuration likewise combines all three attributes.

- **Capability discovery found nothing, so every option showed as unsupported.**
  The Dell classes declare the allowed-values property as **`PossibleValue`**
  (singular, a `StringArray`). The app looked for `PossibleValues`, found no such
  property, and fell back to "show all documented modes" — which hid the
  firmware's real list. `PossibleValue` is now read first, with the plural as a
  fallback, and an integer attribute reports its `LowerBound`/`UpperBound` range
  instead of nothing. On the target machine this turned
  `charge modes: none` into the true
  `Adaptive, Standard, Express, PrimAcUse, Custom`.

- **"Your model predates WMI-ACPI support" was the wrong diagnosis.** On this
  laptop the namespace exists and the interface is present, but an unelevated
  process is refused with `"Access denied"` — a completely different fix. The
  probe now records `access_denied` and `namespace_exists` explicitly, and
  `doctor` reports *"the interface exists but Windows denied access; this needs
  administrator rights"* instead of sending people to look for a BIOS setting.

- **The GUI could permanently wedge itself.** `root.after()` raises
  `RuntimeError: main thread is not in main loop` when the main thread is not
  running Tk's loop. The worker thread's delivery call was wrapped in a bare
  `except: pass`, which left `_busy = True` forever, so after the first failure
  every later request was refused with *"Still working on the previous
  request..."* and the window never updated again. The failure is now logged and
  the busy flag cleared. A pending refresh callback is also cancelled on close,
  which stops a traceback from being printed after the window is destroyed.

- **`doctor` reported an absent thermal attribute as a warning.** When a BIOS has
  no `ThermalManagement` at all — as on the XPS 13 7390 2-in-1 — that is a
  firmware limitation, not something the user can fix, so it is now reported as
  information that says so.

- **The GUI offered thermal profiles the firmware does not have.** Found from a
  screenshot of the real window: all four thermal radio buttons were enabled and
  clickable, and choosing one produced a raw
  `Setting ThermalManagement=UltraPerformance failed: Failed (status 1)`, while
  the button stayed moved on a setting that had never taken effect. The
  capability rule was "only touch the buttons when the supported list is
  non-empty", so an *empty* list disabled nothing. The root cause was that
  `Capabilities` could not distinguish two different situations: an attribute
  that is **absent**, and one that is **present but did not report its allowed
  values**. It now records `supports_thermal` / `supports_charge` explicitly
  (`False` = absent, `None` = could not tell), so the GUI disables an absent
  feature and explains why in the hint, `profile --list` marks all four as
  unsettable with a reason, and a refused change puts the radio button back. The
  fail-open rule is preserved where it belongs: an unreadable value list still
  shows every documented mode.

- **Elevation reported success for a command that never ran.** Caught by checking
  the exit code rather than trusting the output. The elevated command was quoted
  onto a `cmd /c "..."` line; because the interpreter path contains spaces, cmd
  stripped the outer quotes, resolved the whole first token as a filename, failed
  with "The system cannot find the file specified" — and **exited 0**. A refused
  or impossible write therefore looked like a successful one. The command now
  travels in a JSON request file handed to the new `xpshelper.elevated_run`,
  which runs it via `subprocess` (no shell), writes stdout/stderr and the exit
  code to files, and writes the exit-code file **last** as the completion
  sentinel. Verified both ways: an impossible profile change now exits 1, and a
  successful one exits 0.

- **A cold start took ~20 seconds and the window sat "busy" throughout.**
  Enumerating `EnumerationAttribute` costs ~4.2 s on this firmware — it returns
  all 92 instances and `-Filter` does not avoid the walk — and the app enumerated
  it twice per start and re-probed on every periodic refresh. Custom charge
  thresholds are now taken from the snapshot already in hand instead of a second
  PowerShell launch, and the GUI re-probes only on its first refresh. A warm
  `status` went from 14.8 s to 1.6 s and the window settles in ~9 s.

### Added

- **`xps-helper --elevate <command>`** and a **Restart as administrator** button
  in the GUI. Writing a BIOS setting needs elevation, and telling the user to go
  and find an admin prompt is a poor answer; the app raises itself through the
  normal UAC prompt instead. Nothing is bypassed, the exact original argument
  vector is forwarded (`--config`, `--backend` and all), a dismissed prompt is
  reported as a decline, and **a write that cannot be confirmed is never reported
  as success**. Two Windows facts dictated the implementation, both established
  by experiment: `SEE_MASK_NOCLOSEPROCESS` is not honoured for the `runas` verb
  (no process handle is returned, so the elevated child is waited on via an
  exit-code sentinel it writes last), and an elevated process does not inherit
  the parent's stdout pipe (so its output is written to files that the parent
  then echoes).

- **`scripts/verify_hardware.ps1`** — prints what this machine's firmware really
  exposes: the namespace children, which provider answers, every attribute the
  app uses with its permitted values, the BIOS password state, and with
  `-TestWrite` an end-to-end write check that rewrites each attribute's current
  value to itself.

### Changed

- The legacy DCOM provider is no longer used for **writes**, only for reads.
  `Invoke-WmiMethod` binds positionally and cannot construct the `UInt8Array`
  that `SecHandle` requires, failing with *"Unable to cast object of type
  'System.Int32' to type 'System.Array'"*. Rather than ship a second write path
  that never worked, writes go through CIM only, and the script says why.
- `README.md` no longer claims thermal profiles as a working feature. On the
  tested model the firmware exposes 92 enumeration attributes and **none is a
  thermal profile**, so that feature cannot work there.

### Tests

**325 tests** (up from 266). New coverage includes the `PossibleValue` property
name, integer-bound ranges, access-denied versus missing-namespace reporting, the
three-attribute custom-charge write, partial-batch failure, the mandatory
security handle, cmd quoting of a BIOS password, relaunch-command construction
for frozen and source builds, and regression guards for the GUI busy-flag wedge,
the composite-backend unwrapping that hid the elevation prompt, the
attribute-absent versus value-list-unreadable distinction, and the elevated
runner's exit-code and sentinel behaviour (including a command that cannot start
at all).

The GUI capability tests use stand-in widgets rather than real Tk objects, so the
suite stays runnable on a machine with no display or a half-configured Tcl.

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
