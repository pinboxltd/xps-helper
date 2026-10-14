// Read and set the thermal mode through Dell's own DA path, using the exact
// decode recovered from ThermalTables::Parse.
//
// Response layout (byte offsets in the returned 32768-byte buffer):
//   20  cbRes1  -> Error; must be 0
//   24  cbRes2 byte 0 -> SupportedThermalModes (valid 0..15)
//   28  cbRes3 byte 0 -> CurrentThermalMode    (valid 0..8)
//   31                -> FanFailureMode
//
// The frame is DA_CALL_STANDARD_INTERFACE, 44 bytes, at offset 0:
//   u16 Class=17, u16 Selector=19, u32 cbArg1 (0 = read, 1 = write), u32 cbArg2 = mode
//
// Read first (changes nothing). The write form is only issued with --set, so the
// read path can be verified without touching the firmware.
using System;
using System.IO;
using System.Linq;
using System.Reflection;

internal static class Program
{
    private const string DellDir = @"C:\Program Files\Dell\CommandPowerManager";
    private const BindingFlags Any =
        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;

    private static Assembly Resolve(object sender, ResolveEventArgs e)
    {
        var simple = e.Name.Split(',')[0];
        if (simple != "SmbLib" && simple != "ObjLib" && simple != "SystemInterop"
            && simple != "ComponentService" && simple != "Utilities"
            && simple != "Alerts" && simple != "Clarity") return null;
        var p = Path.Combine(DellDir, simple + ".dll");
        return File.Exists(p) ? Assembly.LoadFrom(p) : null;
    }

    private static MethodInfo _exec;
    private static object _da;
    private static object _wd;

    private static byte[] Run(ushort cls, ushort selector, uint arg1, uint arg2, out bool ok)
    {
        ok = false;
        byte[] buf = new byte[32768];
        BitConverter.GetBytes(cls).CopyTo(buf, 0);
        BitConverter.GetBytes(selector).CopyTo(buf, 2);
        BitConverter.GetBytes(arg1).CopyTo(buf, 4);
        BitConverter.GetBytes(arg2).CopyTo(buf, 8);
        BitConverter.GetBytes(0xFFFFFFFDu).CopyTo(buf, 20);   // cbRes1 = -3, as Dell pre-seeds

        object[] call = { _wd, buf };
        try
        {
            var ret = _exec.Invoke(_da, call);
            ok = ret is bool b && b;
        }
        catch (TargetInvocationException ex)
        {
            var e = ex.InnerException ?? ex;
            Console.WriteLine("  EXC " + e.GetType().Name + ": " + e.Message);
            return null;
        }
        return (byte[])call[1];
    }

    private static void Dump(byte[] buf, string label)
    {
        Console.WriteLine(label + ":");
        Console.WriteLine("  Error (cbRes1 @20)          = " + BitConverter.ToUInt32(buf, 20));
        Console.WriteLine("  SupportedThermalModes (@24) = 0x" + buf[24].ToString("x2")
                          + "  (bits: " + Convert.ToString(buf[24], 2).PadLeft(8, '0') + ")");
        Console.WriteLine("  CurrentThermalMode    (@28) = " + buf[28]);
        Console.WriteLine("  FanFailureMode        (@31) = " + buf[31]);
    }

    private static int Main(string[] args)
    {
        int setIdx = Array.IndexOf(args, "--set");
        bool doSet = setIdx >= 0;
        uint target = 0;
        if (doSet && setIdx + 1 < args.Length) uint.TryParse(args[setIdx + 1], out target);

        AppDomain.CurrentDomain.AssemblyResolve += Resolve;
        var smb = Assembly.LoadFrom(Path.Combine(DellDir, "SmbLib.dll"));
        var daT = smb.GetType("Dell.CommandPowerManager.DataAbstraction.DataAbstractor");
        var wdT = smb.GetType("Dell.CommandPowerManager.Wmi.WmiDescriptor");

        _da = Activator.CreateInstance(daT);
        _wd = daT.GetMethods(Any).First(m => m.Name == "Initialize").Invoke(_da, null);
        _exec = daT.GetMethods(Any).First(m => m.Name == "ExecuteDACommand");
        Console.WriteLine("descriptor: " + wdT.GetProperty("Vendor").GetValue(_wd)
                          + " v" + wdT.GetProperty("Version").GetValue(_wd)
                          + " buffer=" + wdT.GetProperty("BufferLength").GetValue(_wd));
        Console.WriteLine();

        Console.WriteLine("=== READ (Class=17 Selector=19 cbArg1=0) ===");
        var read = Run(17, 19, 0, 0, out bool ok1);
        if (read == null) return 1;
        Console.WriteLine("  returned " + ok1);
        Dump(read, "  parsed");

        if (!doSet) return 0;

        Console.WriteLine();
        Console.WriteLine("=== WRITE (Class=17 Selector=19 cbArg1=1 cbArg2=" + target + ") ===");
        var write = Run(17, 19, 1, target, out bool ok2);
        if (write == null) return 1;
        Console.WriteLine("  returned " + ok2);
        Dump(write, "  parsed");

        Console.WriteLine();
        Console.WriteLine("=== READ BACK ===");
        var after = Run(17, 19, 0, 0, out bool ok3);
        if (after != null)
        {
            Console.WriteLine("  returned " + ok3);
            Dump(after, "  parsed");
            Console.WriteLine();
            Console.WriteLine("RESULT: mode is now " + after[28] + " (wrote " + target + ") -> "
                              + (after[28] == target ? "MATCH" : "NO CHANGE"));
        }
        return 0;
    }
}
