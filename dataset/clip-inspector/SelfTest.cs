using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.LZO;
using GBX.NET.ZLib;
using System.Text.Json;

internal static class SelfTest
{
    public static int Run(string[] args)
    {
        if(args.Length!=3){Console.Error.WriteLine("Usage: TrackmaniaClipInspector --self-test-map SOURCE.Map.Gbx NEW_OUTPUT_DIR");return 2;}
        try {
            if(Directory.Exists(args[2]))throw new InvalidDataException("Self-test output directory exists.");Directory.CreateDirectory(args[2]);
            Gbx.LZO=new Lzo();Gbx.ZLib=new ZLib();int count=0;
            void Check(string name,Action<CGameCtnChallenge> mutate,bool expected){
                var original=Gbx.ParseNode<CGameCtnChallenge>(args[1]);var changed=Gbx.ParseNode<CGameCtnChallenge>(args[1]);mutate(changed);
                var result=MapSemantics.Compare(original,changed,Path.GetFullPath(Path.Combine(args[2],name)));
                bool passed=(bool)result.GetType().GetProperty("passed")!.GetValue(result)!;
                if(passed!=expected)throw new InvalidDataException($"Semantic test failed: {name}");
                File.WriteAllText(Path.Combine(args[2],name+".result.json"),JsonSerializer.Serialize(new{synthetic_test=true,name,expected,actual=passed,test_passed=true},new JsonSerializerOptions{WriteIndented=true}));count++;
            }
            Check("identical_map",_=>{},true);
            Check("changed_coordinate",m=>{var b=m.GetBlocks().First();b.Coord=new Int3(b.Coord.X+1,b.Coord.Y,b.Coord.Z);},false);
            Check("changed_variant_flag",m=>m.GetBlocks().First().Flags^=1,false);
            Check("changed_direction",m=>m.GetBlocks().First().Direction=(Direction)(((int)m.GetBlocks().First().Direction+1)%4),false);
            Check("changed_script",m=>m.ChallengeScript="synthetic changed script for rejection test",false);
            Check("changed_decal",m=>m.GetBlocks().First().DecalId="AssignedImportant",false);
            Console.WriteLine($"{count} semantic-map tests passed (synthetic changes never written as game maps).");return 0;
        }catch(Exception ex){Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
    }
}
