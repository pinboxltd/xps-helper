// Full IL disassembler for the methods that build the USTT frame.
//
// The constants-only reader was not enough: reconstructing the frame requires
// knowing which opcode consumes which constant. This walks every instruction and
// prints it with its operand, resolving call targets and string literals.
//
// Opcode metadata comes from System.Reflection.Emit.OpCodes, so operand sizes are
// authoritative rather than a hand-maintained table.
using System.Reflection;
using System.Reflection.Emit;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;

var asmPath = args[0];
var typeFilter = args.Length > 1 ? args[1] : null;
var methodFilter = args.Length > 2 ? args[2] : null;

// Build opcode lookup: single-byte keyed by byte, two-byte keyed by (0xFE, byte).
var oneByte = new Dictionary<byte, OpCode>();
var twoByte = new Dictionary<byte, OpCode>();
foreach (var f in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
{
    if (f.GetValue(null) is not OpCode oc) continue;
    var v = unchecked((ushort)oc.Value);
    if (v <= 0xFF) oneByte[(byte)v] = oc;
    else if ((v & 0xFF00) == 0xFE00) twoByte[(byte)(v & 0xFF)] = oc;
}

using var fs = File.OpenRead(asmPath);
using var pe = new PEReader(fs);
var md = pe.GetMetadataReader();

string TypeName(TypeDefinitionHandle h)
{
    var td = md.GetTypeDefinition(h);
    var ns = md.GetString(td.Namespace);
    var nm = md.GetString(td.Name);
    return string.IsNullOrEmpty(ns) ? nm : ns + "." + nm;
}

string ResolveToken(int tok, bool isString = false)
{
    // User strings live in a separate token table (0x70), so they are not
    // reachable through EntityHandle and must be handled first.
    if (isString || (tok >>> 24) == 0x70)
    {
        try { return "\"" + md.GetUserString(MetadataTokens.UserStringHandle(tok & 0x00FFFFFF)) + "\""; }
        catch { return "\"<bad string token>\""; }
    }
    try
    {
        var h = MetadataTokens.EntityHandle(tok);
        switch (h.Kind)
        {
            case HandleKind.MemberReference:
            {
                var mr = md.GetMemberReference((MemberReferenceHandle)h);
                string owner = mr.Parent.Kind switch
                {
                    HandleKind.TypeReference => md.GetString(md.GetTypeReference((TypeReferenceHandle)mr.Parent).Name),
                    HandleKind.TypeDefinition => TypeName((TypeDefinitionHandle)mr.Parent),
                    _ => "?"
                };
                return owner + "::" + md.GetString(mr.Name);
            }
            case HandleKind.MethodDefinition:
            {
                var mdh = (MethodDefinitionHandle)h;
                var mdf = md.GetMethodDefinition(mdh);
                return md.GetString(mdf.Name);
            }
            case HandleKind.FieldDefinition:
                return md.GetString(md.GetFieldDefinition((FieldDefinitionHandle)h).Name);
            case HandleKind.TypeReference:
                return md.GetString(md.GetTypeReference((TypeReferenceHandle)h).Name);
            case HandleKind.TypeDefinition:
                return TypeName((TypeDefinitionHandle)h);
            // HandleKind.UserString is unreachable here: it is not an
            // EntityHandle, and was handled above via MetadataTokens.
            case HandleKind.MethodSpecification:
                return "<generic method>";
            default:
                return h.Kind.ToString();
        }
    }
    catch { return "?"; }
}

int shown = 0;
foreach (var tdh in md.TypeDefinitions)
{
    var td = md.GetTypeDefinition(tdh);
    var full = TypeName(tdh);
    if (typeFilter != null && !full.Contains(typeFilter, StringComparison.OrdinalIgnoreCase)) continue;

    foreach (var mh in td.GetMethods())
    {
        var mdef = md.GetMethodDefinition(mh);
        var mname = md.GetString(mdef.Name);
        if (methodFilter != null && !mname.Equals(methodFilter, StringComparison.OrdinalIgnoreCase)) continue;
        if (mdef.RelativeVirtualAddress == 0) continue;

        Console.WriteLine($"===== {full}::{mname} =====");
        var il = pe.GetMethodBody(mdef.RelativeVirtualAddress).GetILBytes();
        int i = 0;
        while (i < il.Length)
        {
            int start = i;
            int opSize;
            OpCode oc;
            if (il[i] == 0xFE && i + 1 < il.Length)
            {
                if (!twoByte.TryGetValue(il[i + 1], out oc)) { Console.WriteLine($"  IL_{start:x4}: <unknown fe {il[i + 1]:x2}>"); break; }
                opSize = 2;
            }
            else
            {
                if (!oneByte.TryGetValue(il[i], out oc)) { Console.WriteLine($"  IL_{start:x4}: <unknown {il[i]:x2}>"); break; }
                opSize = 1;
            }
            int p = i + opSize;
            string operand = "";

            switch (oc.OperandType)
            {
                case OperandType.InlineNone:
                    break;
                case OperandType.ShortInlineI:
                case OperandType.ShortInlineVar:
                case OperandType.ShortInlineBrTarget:
                    if (p < il.Length) { operand = (sbyte)il[p] + (oc == OpCodes.Ldc_I4_S ? "" : $" -> IL_{p + 1 + (sbyte)il[p]:x4}"); p += 1; }
                    break;
                case OperandType.InlineVar:
                    if (p + 1 < il.Length) { operand = BitConverter.ToUInt16(il, p).ToString(); p += 2; }
                    break;
                case OperandType.InlineI:
                case OperandType.InlineBrTarget:
                    if (p + 3 < il.Length)
                    {
                        int v = BitConverter.ToInt32(il, p);
                        operand = oc == OpCodes.Ldc_I4 ? v.ToString() : $"IL_{p + 4 + v:x4}";
                        p += 4;
                    }
                    break;
                case OperandType.InlineI8:
                    if (p + 7 < il.Length) { operand = BitConverter.ToInt64(il, p).ToString(); p += 8; }
                    break;
                case OperandType.ShortInlineR:
                    if (p + 3 < il.Length) { operand = BitConverter.ToSingle(il, p).ToString("R"); p += 4; }
                    break;
                case OperandType.InlineR:
                    if (p + 7 < il.Length) { operand = BitConverter.ToDouble(il, p).ToString("R"); p += 8; }
                    break;
                case OperandType.InlineString:
                case OperandType.InlineField:
                case OperandType.InlineMethod:
                case OperandType.InlineType:
                case OperandType.InlineTok:
                case OperandType.InlineSig:
                    if (p + 3 < il.Length)
                    {
                        int tok = BitConverter.ToInt32(il, p);
                        operand = ResolveToken(tok, oc.OperandType == OperandType.InlineString);
                        p += 4;
                    }
                    break;
                case OperandType.InlineSwitch:
                    if (p + 3 < il.Length)
                    {
                        int n = BitConverter.ToInt32(il, p);
                        operand = $"switch[{n}]";
                        p += 4 + n * 4;
                    }
                    break;
                default:
                    operand = $"?{oc.OperandType}";
                    break;
            }

            Console.WriteLine($"  IL_{start:x4}: {oc.Name,-14} {operand}");
            i = p;
        }
        Console.WriteLine();
        shown++;
    }
}
Console.WriteLine($"methods: {shown}");
return 0;
