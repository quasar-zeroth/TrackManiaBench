// Isolated read-only diagnostic. Not a production extractor or clock verifier.
using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.Engines.Scene;
using GBX.NET.LZO;
using GBX.NET.ZLib;

if (args.Length != 1) throw new ArgumentException("PINNED_REQUESTS.json required");
using var requests = JsonDocument.Parse(File.ReadAllText(args[0]));
Gbx.LZO = new Lzo(); Gbx.ZLib = new ZLib();
string Sha(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
foreach (var request in requests.RootElement.GetProperty("files").EnumerateArray())
{
    var path=request.GetProperty("path").GetString()!;
    var expected=request.GetProperty("sha256").GetString()!;
    var ordinal=request.GetProperty("ordinal").GetInt32();
    try {
        if(Sha(File.ReadAllBytes(path))!=expected)throw new InvalidDataException("Pinned ghost changed");
        var parsed=Gbx.Parse(path,new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false,
            MaxDataSize=128*1024*1024,MaxUncompressedBodySize=128*1024*1024});
        var ghost=parsed.Node as CGameCtnGhost ?? throw new InvalidDataException("Original ghost required");
        var rec=ghost.RecordData ?? throw new InvalidDataException("RecordData required");
        var selection=ghost.Chunks.Get<CGameCtnGhost.Chunk03092000>()?.U02
            ?? throw new InvalidDataException("Explicit ghost selection required");
        if(selection.Length==0 || selection.Distinct().Count()!=selection.Length || selection.Any(i=>i<0||i>=rec.EntList.Count))
            throw new InvalidDataException("Invalid/empty ghost record selection");
        var entities=selection.Select(index=>{
            var e=rec.EntList[index];var desc=rec.EntRecordDescs[e.Type];
            if(desc.ClassId!=0x0A018000)throw new InvalidDataException("Selection is not a vehicle entity");
            var samples=e.Samples.Select((s,i)=>(s,i)).Where(x=>x.s is CSceneVehicleVis.EntRecordDelta && x.s.Data.Length>0).Select(x=>{
                var s=(CSceneVehicleVis.EntRecordDelta)x.s;
                var raw=(CSceneVehicleVis.IEntRecordDeltaRawData)s;
                if(raw.Steer!=s.Data[14] || raw.U15!=s.Data[15] || raw.Brake!=s.Data[18])
                    throw new InvalidDataException("Named controls disagree with decoded raw sample bytes");
                return new {sample_index=x.i,record_time_ms=s.Time.TotalMilliseconds,
                    steer=s.Steer,gas=s.Gas,brake=s.Brake,steer_byte=raw.Steer,gas_difference_byte=raw.U15,brake_byte=raw.Brake,
                    raw_sample_bytes=s.Data.Length,raw_sample_sha256=Sha(s.Data),
                    raw_sample_base64=Convert.ToBase64String(s.Data)};
            }).ToArray();
            return new {entity_index=index,e.SceneUId,entity_type=e.Type,class_id=$"0x{desc.ClassId:X8}",
                descriptor_properties=desc.GetType().GetProperties().ToDictionary(p=>p.Name,p=>p.GetValue(desc)?.ToString()),
                begin_ms=e.BeginTime.TotalMilliseconds,end_ms=e.EndTime.TotalMilliseconds,samples};
        }).ToArray();
        Console.WriteLine(JsonSerializer.Serialize(new {schema_version=1,status="selected_vehicle_controls_diagnostic_only",ordinal,
            source_path=path,source_sha256=expected,ghost_login=ghost.GhostLogin,finish_time_ms=ghost.RaceTime?.TotalMilliseconds,
            game_build=ghost.Validate_ExeVersion,record_start_ms=rec.Start.TotalMilliseconds,record_end_ms=rec.End.TotalMilliseconds,
            record_chunks=rec.Chunks.Select(c=>new{id=$"0x{c.Id:X8}",version=c.GetType().GetProperty("Version")?.GetValue(c)}),
            selected_entity_indices=selection,entities,clock_mapping_verified=false}));
    } catch(Exception ex) {Console.WriteLine(JsonSerializer.Serialize(new {ordinal,source_path=path,source_sha256=expected,status="diagnostic_failed",error=ex.ToString()}));}
}
