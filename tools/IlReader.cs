// Focused IL decompiler for Dell's managed assemblies.
//
// Why: the thermal-mode command construction lives in ObjLib.dll and we need to
// read it. A full decompiler (ILSpy) is unnecessary; what matters is being able
// to find a method and read its string literals and call targets.
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;
using System.Text;

var asmPath = args.Length > 0 ? args[0] : throw new ArgumentException("need assembly path");
var filter = args.Length > 1 ? args[1] : null;

using var fs = File.OpenRead(asmPath);
using var pe = new PEReader(fs);
var md = pe.GetMetadataReader();

// --types: just list every TypeDef with its method names.
if (filter == "--types")
{
    Console.WriteLine($"=== {Path.GetFileName(asmPath)}: {md.TypeDefinitions.Count} types ===");
    foreach (var tdh in md.TypeDefinitions)
    {
        var td = md.GetTypeDefinition(tdh);
        var ns = md.GetString(td.Namespace);
        var nm = md.GetString(td.Name);
        Console.WriteLine($"  {(string.IsNullOrEmpty(ns) ? "" : ns + ".")}{nm}");
        foreach (var mh in td.GetMethods())
            Console.WriteLine($"       .{md.GetString(md.GetMethodDefinition(mh).Name)}");
    }
    return 0;
}

Console.WriteLine($"=== {Path.GetFileName(asmPath)} ===");
Console.WriteLine($"types: {md.TypeDefinitions.Count}");

// Map MethodDefinitionHandle -> TypeDefinitionHandle for context.
var methodOwner = new Dictionary<MethodDefinitionHandle, TypeDefinitionHandle>();
foreach (var tdh in md.TypeDefinitions)
{
    var td = md.GetTypeDefinition(tdh);
    foreach (var mh in td.GetMethods())
        methodOwner[mh] = tdh;
}

string TypeName(TypeDefinitionHandle tdh)
{
    var td = md.GetTypeDefinition(tdh);
    var ns = md.GetString(td.Namespace);
    var nm = md.GetString(td.Name);
    return string.IsNullOrEmpty(ns) ? nm : ns + "." + nm;
}

string MemberName(EntityHandle h)
{
    switch (h.Kind)
    {
        case HandleKind.MethodDefinition:
            return md.GetString(md.GetMethodDefinition((MethodDefinitionHandle)h).Name);
        case HandleKind.MemberReference:
            var mr = md.GetMemberReference((MemberReferenceHandle)h);
            return md.GetString(mr.Name);
        case HandleKind.FieldDefinition:
            return md.GetString(md.GetFieldDefinition((FieldDefinitionHandle)h).Name);
        case HandleKind.TypeReference:
            return md.GetString(md.GetTypeReference((TypeReferenceHandle)h).Name);
        case HandleKind.TypeDefinition:
            return TypeName((TypeDefinitionHandle)h);
        case HandleKind.TypeSpecification:
            return "<typespec>";
        default:
            return h.Kind.ToString();
    }
}

// Resolve a TypeRef to something readable (namespace.name).
string TypeRefName(EntityHandle h)
{
    try
    {
        if (h.Kind == HandleKind.TypeReference)
        {
            var tr = md.GetTypeReference((TypeReferenceHandle)h);
            var ns = md.GetString(tr.Namespace);
            var nm = md.GetString(tr.Name);
            return string.IsNullOrEmpty(ns) ? nm : ns + "." + nm;
        }
        if (h.Kind == HandleKind.TypeDefinition) return TypeName((TypeDefinitionHandle)h);
    }
    catch { }
    return "<type>";
}

int shown = 0;
foreach (var tdh in md.TypeDefinitions)
{
    var td = md.GetTypeDefinition(tdh);
    var full = TypeName(tdh);

    foreach (var mh in td.GetMethods())
    {
        var mdef = md.GetMethodDefinition(mh);
        var mname = md.GetString(mdef.Name);

        if (filter != null &&
            !full.Contains(filter, StringComparison.OrdinalIgnoreCase) &&
            !mname.Contains(filter, StringComparison.OrdinalIgnoreCase))
            continue;

        if (mdef.RelativeVirtualAddress == 0) continue;   // no body (abstract/extern)

        Console.WriteLine();
        Console.WriteLine($"----- {full}::{mname} -----");

        try
        {
            var body = pe.GetMethodBody(mdef.RelativeVirtualAddress);
            var il = body.GetILBytes();
            var strings = new List<string>();
            var calls = new List<string>();
            // Operand constants matter as much as the calls: the interface-buffer
            // layout is expressed as ldc.i4 offsets and lengths, so a reader that
            // only reports call targets cannot reconstruct it.
            var consts = new List<string>();

            for (int i = 0; i < il.Length; i++)
            {
                byte op = il[i];
                if (op == 0x72 && i + 4 < il.Length)   // ldstr
                {
                    int tok = BitConverter.ToInt32(il, i + 1);
                    try
                    {
                        var sh = MetadataTokens.UserStringHandle(tok & 0x00FFFFFF);
                        strings.Add(md.GetUserString(sh));
                    }
                    catch { }
                    i += 4;
                }
                // --- integer/float constants ---------------------------------
                else if (op == 0x16) { consts.Add("0"); }
                else if (op == 0x17) { consts.Add("1"); }
                else if (op == 0x18) { consts.Add("2"); }
                else if (op == 0x19) { consts.Add("3"); }
                else if (op == 0x1A) { consts.Add("4"); }
                else if (op == 0x1B) { consts.Add("5"); }
                else if (op == 0x1C) { consts.Add("6"); }
                else if (op == 0x1D) { consts.Add("7"); }
                else if (op == 0x1E) { consts.Add("8"); }
                else if (op == 0x1F && i + 2 < il.Length) { consts.Add(((sbyte)il[i + 1]).ToString()); i += 1; }
                else if (op == 0x20 && i + 5 < il.Length)  // ldc.i4
                { consts.Add(BitConverter.ToInt32(il, i + 1).ToString()); i += 4; }
                else if (op == 0x21 && i + 9 < il.Length)  // ldc.i8
                { consts.Add(BitConverter.ToInt64(il, i + 1).ToString()); i += 8; }
                else if (op == 0x22 && i + 5 < il.Length)  // ldc.r4
                { consts.Add(BitConverter.ToSingle(il, i + 1).ToString()); i += 4; }
                else if (op == 0x23 && i + 9 < il.Length)  // ldc.r8
                { consts.Add(BitConverter.ToDouble(il, i + 1).ToString()); i += 8; }
                else if ((op == 0x28 || op == 0x6F) && i + 4 < il.Length)  // call / callvirt
                {
                    int tok = BitConverter.ToInt32(il, i + 1);
                    try
                    {
                        var h = MetadataTokens.EntityHandle(tok);
                        string owner = "";
                        if (h.Kind == HandleKind.MemberReference)
                        {
                            var mr = md.GetMemberReference((MemberReferenceHandle)h);
                            owner = TypeRefName(mr.Parent) + "::";
                        }
                        else if (h.Kind == HandleKind.MethodDefinition)
                        {
                            var mdh = (MethodDefinitionHandle)h;
                            if (methodOwner.TryGetValue(mdh, out var o))
                                owner = TypeName(o) + "::";
                        }
                        calls.Add(owner + MemberName(h));
                    }
                    catch { }
                    i += 4;
                }
                else if (op == 0x7B || op == 0x7D || op == 0x7E || op == 0x80)  // field ops
                {
                    // skip token
                    i += 4;
                }
                else if (op == 0xFE && i + 1 < il.Length)
                {
                    i += 1;
                }
            }

            if (strings.Count > 0)
            {
                Console.WriteLine("  strings:");
                foreach (var s in strings.Distinct().Take(40))
                    Console.WriteLine($"     \"{s}\"");
            }
            if (consts.Count > 0)
            {
                // Order and repetition matter here: an offset of 4 followed by a
                // length of 1 is the buffer layout.
                Console.WriteLine("  constants (in IL order):");
                Console.WriteLine("     " + string.Join(", ", consts.Take(120)));
            }
            if (calls.Count > 0)
            {
                Console.WriteLine("  calls:");
                foreach (var c in calls.Distinct().Take(50))
                    Console.WriteLine($"     {c}");
            }
            if (strings.Count == 0 && calls.Count == 0 && consts.Count == 0)
                Console.WriteLine($"  (no strings/consts/calls; {il.Length} bytes IL)");

            shown++;
            if (shown > 200) { Console.WriteLine("... truncated"); return 0; }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"  <error reading body: {ex.Message}>");
        }
    }
}

Console.WriteLine();
Console.WriteLine($"methods shown: {shown} (filter: {filter ?? "none"})");
return 0;
