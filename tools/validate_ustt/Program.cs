// Find the working USTT command by using cbRes1 as an oracle.
//
// cbRes1 is a status OUTPUT, not an input: Dell pre-seeds -3, and the firmware
// overwrites offset 20 with 0 on success. Dell's ErrorEnum maps -3 to
// "unsupported", so:
//     cbRes1 == 0            -> command accepted
//     cbRes1 == 0xFFFFFFFD   -> command not supported
//
// That turns the interface into an oracle. Enumerate Class/Selector/cbArg1 and
// report which combinations the firmware accepts, then look at whether any
// accepted combination also returns a payload.
//
// READ-FIRST POLICY: only Class/Selector/cbArg1=0 (read) probes are sent by
// default. A successful read changes nothing; the write form (cbArg1=1) is only
// attempted with --write and a single explicit mode value.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Management;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential)]
internal struct DaCallStandardInterface
{
    public ushort Class;
    public ushort Selector;
    public uint cbArg1;
    public uint cbArg2;
    public uint cbArg3;
    public uint cbArg4;
    public uint cbRes1;
    public uint cbRes2;
    public uint cbRes3;
    public uint cbRes4;
    public uint ArgAttrib;
    public uint BLength;
}

internal static class Program
{
    private const string Ns = @"root\wmi";
    private const string Instance = @"ACPI\PNP0C14\0_0";
    private const uint NotSupported = 0xFFFFFFFD;

    private static ManagementScope? _scope;
    private static ManagementClass? _bfn;
    private static ManagementObject? _target;
    private static ManagementClass? _bdat;

    private static byte[] StructToBytes<T>(T value) where T : struct
    {
        int size = Marshal.SizeOf<T>();
        IntPtr p = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(value, p, false);
            var bytes = new byte[size];
            Marshal.Copy(p, bytes, 0, size);
            return bytes;
        }
        finally { Marshal.FreeHGlobal(p); }
    }

    private static bool Connect()
    {
        _scope = new ManagementScope($@"\\.\{Ns}");
        _scope.Connect();
        _bfn = new ManagementClass(_scope, new ManagementPath("BFn"), null);
        _bfn.Get();
        foreach (ManagementObject inst in _bfn.GetInstances())
        {
            if (string.Equals(inst["InstanceName"]?.ToString(), Instance, StringComparison.OrdinalIgnoreCase))
            { _target = inst; break; }
            _target ??= inst;
        }
        if (_target == null) return false;
        _bdat = new ManagementClass(_scope, new ManagementPath("BDat"), null);
        _bdat.Get();
        return true;
    }

    /// Sends one frame; returns the response bytes, or null if the call failed.
    private static byte[]? Send(ushort cls, ushort selector, uint arg1, uint arg2, int bufferSize = 32768)
    {
        var frame = new DaCallStandardInterface
        {
            Class = cls,
            Selector = selector,
            cbArg1 = arg1,
            cbArg2 = arg2,
            cbRes1 = NotSupported,
        };
        byte[] buffer = new byte[bufferSize];
        Buffer.BlockCopy(StructToBytes(frame), 0, buffer, 0, 44);

        var inParams = _bfn!.GetMethodParameters("DoBFn");
        using var bdatInst = _bdat!.CreateInstance();
        bdatInst["Bytes"] = buffer;
        inParams["Data"] = bdatInst;

        try
        {
            var outParams = _target!.InvokeMethod("DoBFn", inParams, null);
            if (outParams?["Data"] is ManagementBaseObject dbo && dbo["Bytes"] is byte[] rb) return rb;
            return buffer;
        }
        catch (ManagementException ex)
        {
            Console.WriteLine($"      EXC {ex.ErrorCode}: {ex.Message}");
            return null;
        }
    }

    private static int Main(string[] args)
    {
        bool doWrite = args.Contains("--write");
        if (!Connect()) { Console.WriteLine("no BFn instance"); return 2; }
        Console.WriteLine($"target: {_target!["InstanceName"]}");
        Console.WriteLine();

        // Sanity: confirm the oracle reproduces the known-good read.
        Console.WriteLine("=== control: the documented read (17/19, cbArg1=0) ===");
        var ctrl = Send(17, 19, 0, 0);
        if (ctrl != null)
        {
            uint st = BitConverter.ToUInt32(ctrl, 20);
            Console.WriteLine($"   cbRes1 = 0x{st:x8} ({(st == 0 ? "ACCEPTED" : st == NotSupported ? "unsupported" : "other")})");
        }
        Console.WriteLine();

        Console.WriteLine("=== enumerating Selector for Class=17, cbArg1=0 (read) ===");
        var accepted = new List<(ushort cls, ushort sel, uint arg1, uint arg2, int payload)>();
        for (ushort sel = 0; sel <= 32; sel++)
        {
            var r = Send(17, sel, 0, 0);
            if (r == null) continue;
            uint st = BitConverter.ToUInt32(r, 20);
            int payload = 0;
            for (int i = 44; i < r.Length; i++) if (r[i] != 0) payload++;
            string tag = st == 0 ? "ACCEPTED" : st == NotSupported ? "unsupported" : $"status=0x{st:x8}";
            if (st == 0 || payload > 0)
            {
                Console.WriteLine($"   sel={sel,3}  {tag,-14} payloadBytes={payload}");
                accepted.Add((17, sel, 0, 0, payload));
            }
        }

        Console.WriteLine();
        Console.WriteLine("=== enumerating Class, Selector=19, cbArg1=0 (read) ===");
        for (ushort cls = 0; cls <= 32; cls++)
        {
            var r = Send(cls, 19, 0, 0);
            if (r == null) continue;
            uint st = BitConverter.ToUInt32(r, 20);
            int payload = 0;
            for (int i = 44; i < r.Length; i++) if (r[i] != 0) payload++;
            if (st == 0 || payload > 0)
            {
                Console.WriteLine($"   cls={cls,3}  status=0x{st:x8} payloadBytes={payload}");
                accepted.Add((cls, 19, 0, 0, payload));
            }
        }

        Console.WriteLine();
        Console.WriteLine($"accepted combinations: {accepted.Count}");
        foreach (var a in accepted) Console.WriteLine($"   Class={a.cls} Selector={a.sel} arg1={a.arg1} arg2={a.arg2} payload={a.payload}");

        // If something returns a payload, dump it: that is the thermal table.
        var best = accepted.OrderByDescending(a => a.payload).FirstOrDefault();
        if (best.payload > 0)
        {
            var r = Send(best.cls, best.sel, best.arg1, best.arg2);
            if (r != null)
            {
                Console.WriteLine();
                Console.WriteLine($"=== payload from Class={best.cls} Selector={best.sel} ===");
                for (int i = 0; i < Math.Min(256, r.Length); i += 16)
                {
                    var chunk = r.Skip(i).Take(16).ToArray();
                    if (chunk.All(b => b == 0)) continue;
                    Console.WriteLine($"   {i:x4}  " + string.Join(" ", chunk.Select(b => b.ToString("x2"))));
                }
            }
        }

        if (doWrite)
        {
            Console.WriteLine();
            Console.WriteLine("=== WRITE probe (cbArg1=1), documenting argument byte order ===");
            // Dell builds a 4-byte array: [0] = (byte)newValue, [1] = (byte)aac,
            // then BitConverter.ToInt32 -> cbArg2. So cbArg2 = value | (aac << 8).
            for (uint mode = 0; mode <= 4; mode++)
            {
                var r = Send(17, 19, 1, mode);
                if (r == null) continue;
                uint st = BitConverter.ToUInt32(r, 20);
                Console.WriteLine($"   mode={mode}  cbRes1=0x{st:x8} ({(st == 0 ? "ACCEPTED" : st == NotSupported ? "unsupported" : "other")})");
            }
        }
        return 0;
    }
}
