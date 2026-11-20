using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.LZO;
using GBX.NET.ZLib;
using TmEssentials;

internal static class NativeGhostRebind
{
    public static int Run(string[] args)
    {
        if(args.Length!=5){Console.Error.WriteLine("Usage: TrackmaniaClipInspector --rebind-native-ghost REFERENCE_NATIVE.Clip.Gbx TARGET_NATIVE.Replay.Gbx VERIFIED_COMPARISON.json NEW.Clip.Gbx");return 2;}
        try{
            string source=Path.GetFullPath(args[1]),replayPath=Path.GetFullPath(args[2]),proofPath=Path.GetFullPath(args[3]),output=Path.GetFullPath(args[4]);
            if(File.Exists(output)||File.Exists(output+".provenance.json"))throw new InvalidDataException("Output exists.");
            using var proof=JsonDocument.Parse(File.ReadAllBytes(proofPath));var p=proof.RootElement;
            if(p.GetProperty("status").GetString()!="native_replay_gameplay_sources_verified"||!p.GetProperty("canonical_record_equal").GetBoolean()||!p.GetProperty("semantic_map_verification").GetProperty("passed").GetBoolean()||p.GetProperty("native_replay").GetProperty("sha256").GetString()!=Hash(File.ReadAllBytes(replayPath)))
                throw new InvalidDataException("Target native replay lacks matching verified source/map proof.");
            Gbx.LZO=new Lzo();Gbx.ZLib=new ZLib();var settings=new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false};
            var clip=Gbx.ParseNode<CGameCtnMediaClip>(source,settings);var entities=clip.Tracks.SelectMany(t=>t.Blocks).OfType<CGameCtnMediaBlockEntity>().ToArray();
            var replay=Gbx.ParseNode<CGameCtnReplayRecord>(replayPath,settings);var ghosts=replay.GetGhosts().Distinct(ReferenceEqualityComparer.Instance).Cast<CGameCtnGhost>().ToArray();
            if(entities.Length!=1||ghosts.Length!=1)throw new InvalidDataException("Exactly one native entity and target ghost required.");
            var entity=entities[0];var ghost=ghosts[0];var gc=ghost.Chunks.Get<CGameCtnGhost.Chunk03092000>();
            if(gc?.Version!=9||ghost.AppearanceVersion!=1||ghost.HasBadges||ghost.Badge is not null||gc.U02 is null||gc.U02.Length==0||ghost.RecordData?.CompressedData is null||ghost.RaceTime is null||ghost.PlayerModel is null)
                throw new InvalidDataException("Unsupported target native appearance/record layout. Badges require an independently verified mapping.");
            if(entity.Chunks.Get<CGameCtnMediaBlockEntity.Chunk0329F000>()?.Version!=11||entity.Keys is null||entity.Keys.Count!=2)
                throw new InvalidDataException("Unsupported reference native entity layout.");
            if(ghost.Validate_ChallengeUid?.ToString()!=p.GetProperty("map_uid").GetString()||ghost.RaceTime.Value.TotalMilliseconds!=p.GetProperty("finish_time_ms").GetInt32())
                throw new InvalidDataException("Target replay proof identity mismatch.");
            var payload=ghost.RecordData.CompressedData.Data.ToArray();int endMs=ghost.RecordData.End.TotalMilliseconds;
            if(gc.U02.Any(index=>index<0||index>=ghost.RecordData.EntList.Count))throw new InvalidDataException("Target notice selection outside original record entity list.");
            // Transfer every supported player-specific native field, including the
            // previously missing notice bindings. Keep the complete native scene
            // object/chunks for fixed rendering fields; no source record rewriting.
            entity.RecordData=ghost.RecordData;entity.NoticeRecords=gc.U02.ToArray();entity.PlayerModel=ghost.PlayerModel;
            entity.AppearanceArchiveVersion=ghost.AppearanceVersion;entity.AppearanceColor=ghost.LightTrailColor;
            entity.SkinNames=ghost.SkinPackDescs;entity.SkinOptions=ghost.SkinCustomization;entity.HasBadges=false;entity.Badge=null;
            entity.GhostName=ghost.GhostNickname;entity.RaceTime=ghost.RaceTime;entity.StartOffset=TimeSingle.Zero;entity.TimeScale=1;
            entity.Keys[0].Time=TimeSingle.Zero;entity.Keys[1].Time=TimeSingle.FromMilliseconds(endMs);
            ghost.RecordData.CompressedData.Parsed=false;
            var track=clip.Tracks.Single(t=>t.Blocks.Contains(entity));track.Blocks=[entity];track.Name="Ghost "+ghost.GhostNickname;clip.Tracks=[track];
            using(var f=new FileStream(output,FileMode.CreateNew))new Gbx<CGameCtnMediaClip>(clip).Save(f);
            var reread=Gbx.ParseNode<CGameCtnMediaClip>(output,settings).Tracks.SelectMany(t=>t.Blocks).OfType<CGameCtnMediaBlockEntity>().Single();
            if(!payload.SequenceEqual(reread.RecordData!.CompressedData!.Data)||reread.NoticeRecords is null||!gc.U02.SequenceEqual(reread.NoticeRecords)||reread.SkinOptions!=ghost.SkinCustomization||reread.GhostName!=ghost.GhostNickname||reread.RaceTime!=ghost.RaceTime)
                throw new InvalidDataException("Rebound native entity roundtrip changed target data.");
            object Pin(string path)=>new{path,sha256=Hash(File.ReadAllBytes(path))};
            var result=new{schema_version=1,status="native_scaffold_target_ghost_fields_rebound_native_reference_comparison_required",source_native_clip=Pin(source),target_native_replay=Pin(replayPath),target_source_comparison=Pin(proofPath),output_clip=Pin(output),
                map_uid=ghost.Validate_ChallengeUid?.ToString(),ghost_login=ghost.GhostLogin,ghost_name=ghost.GhostNickname,finish_time_ms=ghost.RaceTime.Value.TotalMilliseconds,
                notice_records=gc.U02,appearance_version=ghost.AppearanceVersion,original_record_data_payload_sha256=Hash(payload),output_record_data_payload_sha256=Hash(reread.RecordData.CompressedData.Data),record_data_payload_byte_equal=true,
                transferred_fields=new[]{"RecordData","NoticeRecords (ghost metadata U02)","PlayerModel","AppearanceArchiveVersion","AppearanceColor (LightTrailColor)","SkinNames (SkinPackDescs)","SkinOptions (SkinCustomization)","HasBadges","Badge","GhostName","RaceTime"},
                preserved_reference_fields="Complete native entity chunks and render flags/key appearance values",unsupported="Badged ghosts and unrecognized appearance/archive versions are rejected",
                native_reference_comparison_verified=false,native_import_verified=false,video_alignment_verified=false};
            using(var f=new FileStream(output+".provenance.json",FileMode.CreateNew))JsonSerializer.Serialize(f,result,new JsonSerializerOptions{WriteIndented=true});
            Console.WriteLine($"Native scaffold rebound without changing target RecordData: {output}");return 0;
        }catch(Exception ex){Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
    }
    static string Hash(byte[] b)=>Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();
}
