using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.Engines.Plug;
using GBX.NET.LZO;
using GBX.NET.ZLib;
using TmEssentials;

internal static class ReplayCompare
{
    public static int Run(string[] args)
    {
        if(args.Length!=5) {Console.Error.WriteLine("Usage: TrackmaniaClipInspector --compare-replay ORIGINAL.Ghost.Gbx NATIVE.Replay.Gbx ORIGINAL.Map.Gbx OUTPUT.json");return 2;}
        try {
            var output=Path.GetFullPath(args[4]);if(File.Exists(output)) throw new InvalidDataException("Output exists.");
            Gbx.LZO=new Lzo();Gbx.ZLib=new ZLib();var settings=new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false};
            var original=Gbx.ParseNode<CGameCtnGhost>(args[1],settings);var replay=Gbx.ParseNode<CGameCtnReplayRecord>(args[2],settings);
            var ghosts=replay.GetGhosts().Distinct(ReferenceEqualityComparer.Instance).Cast<CGameCtnGhost>().ToArray();
            if(ghosts.Length!=1) throw new InvalidDataException("Expected exactly one nested ghost.");var native=ghosts[0];
            if(original.RecordData is null || native.RecordData is null || replay.ChallengeGbxData is null) throw new InvalidDataException("Missing full RecordData or embedded map.");
            var a=RecordBytes(original.RecordData);var b=RecordBytes(native.RecordData);var map=File.ReadAllBytes(args[3]);var embedded=replay.ChallengeGbxData.Data;
            bool identity=original.Validate_ChallengeUid==native.Validate_ChallengeUid && original.GhostLogin==native.GhostLogin && original.RaceTime==native.RaceTime
                && replay.MapInfo?.Id.ToString()==original.Validate_ChallengeUid?.ToString() && replay.Time==original.RaceTime;
            bool record=a.SequenceEqual(b),mapEqual=map.SequenceEqual(embedded);
            var rawA=DecompressRecord(original.RecordData);var rawB=DecompressRecord(native.RecordData);
            var mapA=DecompressGbx(map);var mapB=DecompressGbx(embedded);
            bool rawRecord=rawA.SequenceEqual(rawB),rawMap=mapA.SequenceEqual(mapB);
            using var mapStreamA=new MemoryStream(mapA);using var mapStreamB=new MemoryStream(mapB);
            var mapGbxA=Gbx.Parse(mapStreamA,new GbxReadSettings{ReadRawBody=true});var mapGbxB=Gbx.Parse(mapStreamB,new GbxReadSettings{ReadRawBody=true});
            var bodyA=mapGbxA.Body.RawData.ToArray();var bodyB=mapGbxB.Body.RawData.ToArray();
            bool mapBody=bodyA.SequenceEqual(bodyB);
            File.WriteAllBytes(output+".original-map-body.bin",bodyA);File.WriteAllBytes(output+".native-map-body.bin",bodyB);
            var canonicalA=CanonicalRecord(original.RecordData);var canonicalB=CanonicalRecord(native.RecordData);
            bool canonicalRecord=canonicalA.SequenceEqual(canonicalB);
            File.WriteAllBytes(output+".original-record.json",canonicalA);File.WriteAllBytes(output+".native-record.json",canonicalB);
            File.WriteAllBytes(output+".original-map-uncompressed.Gbx",mapA);File.WriteAllBytes(output+".native-map-uncompressed.Gbx",mapB);
            object Ref(string p)=>new{path=Path.GetFullPath(p),sha256=Hash(File.ReadAllBytes(p)),bytes=new FileInfo(p).Length};
            var semantic=MapSemantics.Compare(Gbx.ParseNode<CGameCtnChallenge>(args[3],settings),replay.Challenge ?? throw new InvalidDataException("Missing parsed map"),output);
            var semanticPass=(bool)semantic.GetType().GetProperty("passed")!.GetValue(semantic)!;
            var proof=new{schema_version=1,semantic_map_verification=semantic,status=identity&&canonicalRecord&&semanticPass?"native_replay_gameplay_sources_verified":"native_replay_difference_detected",
                map_body_byte_equal=mapBody,map_body_original_sha256=Hash(bodyA),map_body_native_sha256=Hash(bodyB),map_body_original_bytes=bodyA.Length,map_body_native_bytes=bodyB.Length,decompressed_record_byte_equal=rawRecord,decompressed_record_original_sha256=Hash(rawA),decompressed_record_native_sha256=Hash(rawB),canonical_record_equal=canonicalRecord,canonical_record_original_sha256=Hash(canonicalA),canonical_record_native_sha256=Hash(canonicalB),decompressed_map_byte_equal=rawMap,decompressed_map_original_sha256=Hash(mapA),decompressed_map_native_sha256=Hash(mapB),original_ghost=Ref(args[1]),native_replay=Ref(args[2]),original_map=Ref(args[3]),core_identity_equal=identity,
                source_record_data_sha256=Hash(a),native_record_data_sha256=Hash(b),full_record_data_roundtrip_bytes_equal=record,serialized_record_note="Standalone GBX.NET wrappers contain timestamped signatures; canonical_record_equal compares actual decoded descriptors, entity raw sample bytes/times, notices and modules instead.",original_record_version=original.RecordData.Chunks.Get<CPlugEntRecordData.Chunk0911F000>()?.Version,native_record_version=native.RecordData.Chunks.Get<CPlugEntRecordData.Chunk0911F000>()?.Version,
                embedded_map_sha256=Hash(embedded),embedded_map_bytes=embedded.Length,embedded_map_byte_equal=mapEqual,
                map_uid=native.Validate_ChallengeUid?.ToString(),ghost_login=native.GhostLogin,account_id=native.GhostLogin is null?null:AccountUtils.ToAccountId(native.GhostLogin).ToString(),finish_time_ms=native.RaceTime?.TotalMilliseconds,
                original_ghost_uid=original.GhostUid?.ToString(),native_ghost_uid=native.GhostUid?.ToString(),serialized_clip_present=replay.Clip is not null,
                input_equivalence_note="Run the immutable action extractor on the replay and compare raw stores/events/held rows against the original ghost. This comparison separately covers all serialized native RecordData and embedded map bytes.",
                video_alignment_verified=false,gbx_net_version="2.5.1"};
            using(var f=new FileStream(output,FileMode.CreateNew))JsonSerializer.Serialize(f,proof,new JsonSerializerOptions{WriteIndented=true});
            Console.WriteLine($"Core identity equal={identity}; full RecordData equal={record}; embedded map byte equal={mapEqual}; proof={output}");return identity&&canonicalRecord&&semanticPass?0:2;
        }catch(Exception ex){Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
    }
    private static byte[] DecompressRecord(CPlugEntRecordData rec){if(rec.CompressedData is null)return RecordBytes(rec);using var a=new MemoryStream(rec.CompressedData.Data);using var b=new MemoryStream();Gbx.ZLib.Decompress(a,b);return b.ToArray();}
    private static byte[] DecompressGbx(byte[] data){using var a=new MemoryStream(data);using var b=new MemoryStream();Gbx.DecompressAsync(a,b).GetAwaiter().GetResult();return b.ToArray();}
    private static byte[] CanonicalRecord(CPlugEntRecordData rec)=>JsonSerializer.SerializeToUtf8Bytes(new{
        start=rec.Start.TotalMilliseconds,end=rec.End.TotalMilliseconds,rec.EntRecordDescs,rec.NoticeRecordDescs,
        entities=rec.EntList.Select(e=>new{e.Type,e.SceneUId,begin=e.BeginTime.TotalMilliseconds,end=e.EndTime.TotalMilliseconds,e.U04,
            samples=e.Samples.Select(s=>new{time=s.Time.TotalMilliseconds,data=Hash(s.Data)}).ToArray(),samples2=e.Samples2.Select(s=>new{time=s.Time.TotalMilliseconds,s.Type,data=Hash(s.Data)}).ToArray()}).ToArray(),
        notices=rec.BulkNoticeList.Select(n=>new{n.Type,time=n.Time.TotalMilliseconds,data=Hash(n.Data)}).ToArray(),rec.CustomModulesDeltaLists
    },new JsonSerializerOptions{WriteIndented=true});
    private static string Hash(byte[] b)=>Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();
    private static byte[] RecordBytes(CPlugEntRecordData data){using var m=new MemoryStream();new Gbx<CPlugEntRecordData>(data){BodyCompression=GbxCompression.Uncompressed}.Save(m);return m.ToArray();}
}
