using System.Collections;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using GBX.NET.Engines.Game;

internal static class MapSemantics
{
    private static readonly JsonSerializerOptions Json=new(){WriteIndented=true};
    public static Dictionary<string,object?> Snapshot(CGameCtnChallenge map)=>new(){
        ["identity_environment_rules"]=Fields(map,"MapInfo","MapName","Decoration","Size","MapType","MapStyle","IsLapRace","NbLaps","NbCheckpoints","Checkpoints","PlayerModel","Mode","ModPackDesc","ChallengeScript"),
        ["blocks"]=map.GetBlocks().Select(Block).ToArray(),
        ["baked_blocks"]=map.GetBakedBlocks().Select(Block).ToArray(),
        ["anchored_objects"]=map.GetAnchoredObjects().Select(i=>Fields(i,"ItemModel","YawPitchRoll","BlockUnitCoord","AnchorTreeId","AbsolutePositionInMap","WaypointSpecialProperty","Flags","PivotPosition","Scale","PackDesc","BoundingBox","BotPathIndex","ScriptParameters","Color","AnimPhaseOffset","ForegroundPackDesc","LightmapQuality")).ToArray(),
        ["script_metadata"]=Value(map.ScriptMetadata,0),
        ["embedded_assets"]=new{zip_sha256=map.EmbeddedZipData is null?null:Hash(map.EmbeddedZipData),external_refs=map.EmbeddedItemModelsExternalDataRefs,expected_models=map.ExpectedEmbeddedItemModels?.Select(x=>x.ToString()).ToArray()}
    };
    private static object Block(CGameCtnBlock b)=>Fields(b,"BlockModel","Direction","Dir","Coord","Flags","Variant","SubVariant","IsGround","IsClip","IsPillar","IsReplacement","IsGhost","IsFree","AbsolutePositionInMap","YawPitchRoll","Skin","WaypointSpecialProperty","Color","LightmapQuality","DecalId");
    private static Dictionary<string,object?> Fields(object o,params string[] fields){var d=new Dictionary<string,object?>();foreach(var name in fields){var prop=o.GetType().GetProperty(name);if(prop is not null)d[name]=Value(prop.GetValue(o),0);}return d;}
    private static object? Value(object? v,int depth){
        if(v is null)return null;if(depth>16)throw new InvalidDataException("Map semantic property graph exceeds supported depth.");
        var type=v.GetType();if(v is string||v is bool||type.IsPrimitive||v is decimal)return v;if(type.IsEnum)return v.ToString();
        if(v is byte[] bytes)return new{bytes=bytes.Length,sha256=Hash(bytes)};
        if(v is IDictionary dict){var d=new SortedDictionary<string,object?>();foreach(DictionaryEntry x in dict)d[Convert.ToString(x.Key)??""]=Value(x.Value,depth+1);return d;}
        if(v is IEnumerable seq)return seq.Cast<object?>().Select(x=>Value(x,depth+1)).ToArray();
        if(type.IsValueType||type.FullName=="GBX.NET.Ident"||type.FullName=="GBX.NET.PackDesc")return v.ToString();
        var values=new SortedDictionary<string,object?>();
        foreach(var p in type.GetProperties(BindingFlags.Instance|BindingFlags.Public).Where(p=>p.GetIndexParameters().Length==0&&p.CanRead&&p.Name is not ("Chunks" or "References" or "GameVersion" or "ClassId"))) values[p.Name]=Value(p.GetValue(v),depth+1);
        return values;
    }
    public static object Compare(CGameCtnChallenge original,CGameCtnChallenge native,string output){
        var a=Snapshot(original);var b=Snapshot(native);var aBytes=JsonSerializer.SerializeToUtf8Bytes(a,Json);var bBytes=JsonSerializer.SerializeToUtf8Bytes(b,Json);
        var aPath=output+".original-map-semantics.json";var bPath=output+".native-map-semantics.json";File.WriteAllBytes(aPath,aBytes);File.WriteAllBytes(bPath,bBytes);
        var normA=JsonNode.Parse(aBytes)!.AsObject();var normB=JsonNode.Parse(bBytes)!.AsObject();
        var normalization=new List<object>();bool allowed=true;
        var rulesA=normA["identity_environment_rules"]!.AsObject();var rulesB=normB["identity_environment_rules"]!.AsObject();
        foreach(string field in new[]{"MapType","MapStyle","NbCheckpoints"}){
            if(JsonNode.DeepEquals(rulesA[field],rulesB[field]))continue;
            bool ok=field switch {
                "MapType" => rulesA[field]?.GetValue<string>()==@"TrackMania\TM_Race" && rulesB[field] is null,
                "MapStyle" => rulesA[field]?.GetValue<string>()=="" && rulesB[field] is null,
                "NbCheckpoints" => rulesB[field]?.GetValue<int>()==0 && rulesA[field]?.GetValue<int>()==original.GetBlocks().Count(x=>x.WaypointSpecialProperty?.Tag is "Goal" or "Checkpoint"),
                _=>false};
            allowed &= ok;normalization.Add(new{kind="omitted_replay_header_metadata",field,original=rulesA[field]?.ToJsonString(),native=rulesB[field]?.ToJsonString(),allowed=ok});
            if(ok)rulesA[field]=rulesB[field]?.DeepClone();
        }
        var blocksA=normA["blocks"]!.AsArray();var blocksB=normB["blocks"]!.AsArray();
        if(blocksA.Count==blocksB.Count)for(int i=0;i<blocksA.Count;i++){
            var x=blocksA[i]!.AsObject();var y=blocksB[i]!.AsObject();
            if(!JsonNode.DeepEquals(x["DecalId"],y["DecalId"])){
                var decal=x["DecalId"]?.GetValue<string>();int flagsX=x["Flags"]!.GetValue<int>(),flagsY=y["Flags"]!.GetValue<int>();
                bool ok=decal=="Unassigned1" && y["DecalId"] is null && (flagsX^flagsY)==(1<<17) && (flagsY&(1<<17))==0;
                allowed &= ok;normalization.Add(new{kind="unassigned_decal_metadata_removed",block_index=i,original_decal=decal,flags_xor=flagsX^flagsY,allowed=ok});
                if(ok){x["DecalId"]=null;x["Flags"]=flagsY;}
            }
            if(x["Skin"] is JsonObject sx && y["Skin"] is JsonObject sy && !JsonNode.DeepEquals(sx["Text"],sy["Text"])){
                string? text=sx["Text"]?.GetValue<string>();bool ok=text is not null && System.Text.RegularExpressions.Regex.IsMatch(text,@"^!\d+$") && sy["Text"]?.GetValue<string>()=="";
                allowed &= ok;normalization.Add(new{kind="advertisement_slot_text_cleared",block_index=i,original_text=text,native_text=sy["Text"]?.GetValue<string>(),allowed=ok});
                if(ok)sx["Text"]="";
            }
        }
        var checks=a.Keys.ToDictionary(k=>k,k=>new{passed=JsonNode.DeepEquals(normA[k],normB[k]),evidence=$"Exact ordered comparison of {k} after only the explicitly listed native serialization metadata normalizations. Original/native unmodified snapshots remain hash-pinned."});
        checks["only_declared_metadata_normalizations"]=new{passed=allowed,evidence="Flags may change only by DecalBit17 when Unassigned1 is removed; skin asset paths remain exact while !digits advertisement slot text clears; omitted header values are checked against source race type and unchanged waypoint tags."};
        return new{passed=allowed&&checks.Values.All(x=>x.passed),checks,evidence_files=new[]{new{path=aPath,sha256=Hash(aBytes)},new{path=bPath,sha256=Hash(bBytes)}},
            observed_non_gameplay_differences=normalization,
            scope="Gameplay geometry/model IDs, ordered block/item placement/rotation/variants/flags/skins/waypoints, map environment/rules/script and embedded assets. Native omission of unassigned decal/ad-slot/header metadata is recorded explicitly. This does not assert identical map bytes or identical advertisement rendering. Camera clips, thumbnails and lightmap cache are outside gameplay geometry equivalence."};
    }

    private static string Hash(byte[] b)=>Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();
}
