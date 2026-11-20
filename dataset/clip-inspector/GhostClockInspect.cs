using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.LZO;
using GBX.NET.ZLib;

internal static class GhostClockInspect
{
    public static int Run(string[] args)
    {
        if(args.Length!=3){Console.Error.WriteLine("Usage: TrackmaniaClipInspector --inspect-ghost-clocks SOURCE.Ghost.Gbx|SOURCE.Replay.Gbx NEW.json");return 2;}
        try{
            string source=Path.GetFullPath(args[1]),output=Path.GetFullPath(args[2]);
            if(File.Exists(output))throw new InvalidDataException("Output exists.");
            Gbx.LZO=new Lzo();Gbx.ZLib=new ZLib();
            var node=Gbx.Parse(source,new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false}).Node;
            var ghosts=node is CGameCtnGhost direct?new[]{direct}:node is CGameCtnReplayRecord replay?replay.GetGhosts().Distinct(ReferenceEqualityComparer.Instance).Cast<CGameCtnGhost>().ToArray():[];
            if(ghosts.Length!=1)throw new InvalidDataException("Exactly one ghost required.");
            var ghost=ghosts[0];var rec=ghost.RecordData??throw new InvalidDataException("Missing RecordData.");
            var chunk=ghost.Chunks.Get<CGameCtnGhost.Chunk03092000>()??throw new InvalidDataException("Missing modern ghost metadata.");
            var events=rec.EntList.SelectMany((entity,entityIndex)=>entity.Samples2.Select((sample,sampleIndex)=>{
                var desc=rec.NoticeRecordDescs[sample.Type];
                var words=Enumerable.Range(0,sample.Data.Length/4).Select(i=>BinaryPrimitives.ReadInt32LittleEndian(sample.Data.AsSpan(i*4,4))).ToArray();
                return new{entity_index=entityIndex,entity_type=entity.Type,entity_class_id=$"0x{rec.EntRecordDescs[entity.Type].ClassId:X8}",
                    sample_index=sampleIndex,record_timestamp_ms=sample.Time.TotalMilliseconds,notice_descriptor_index=sample.Type,
                    notice_class_id=$"0x{desc.ClassId:X8}",notice_type=desc.NoticeType,notice_archive_version=desc.NoticeArchiveVersion,
                    raw_payload_base64=Convert.ToBase64String(sample.Data),raw_payload_sha256=Hash(sample.Data),raw_payload_bytes=sample.Data.Length,
                    little_endian_int32_words_for_investigation=words};
            })).ToArray();
            var result=new{schema_version=1,status="raw_clock_notice_evidence_uninterpreted",source_path=source,source_sha256=Hash(File.ReadAllBytes(source)),
                map_uid=ghost.Validate_ChallengeUid?.ToString(),ghost_login=ghost.GhostLogin,ghost_name=ghost.GhostNickname,finish_time_ms=ghost.RaceTime?.TotalMilliseconds,
                checkpoints=ghost.Checkpoints?.Select(c=>new{race_time_ms=c.Time?.TotalMilliseconds,c.CheckpointId}).ToArray(),
                record_start_ms=rec.Start.TotalMilliseconds,record_end_ms=rec.End.TotalMilliseconds,
                entities=rec.EntList.Select((e,i)=>new{entity_index=i,entity_type=e.Type,entity_class_id=$"0x{rec.EntRecordDescs[e.Type].ClassId:X8}",scene_uid=e.SceneUId,begin_time_ms=e.BeginTime.TotalMilliseconds,end_time_ms=e.EndTime.TotalMilliseconds}).ToArray(),
                appearance=new{ghost.AppearanceVersion,player_model=ghost.PlayerModel?.ToString(),light_trail_color=ghost.LightTrailColor.ToString(),
                    skins=ghost.SkinPackDescs?.Select(s=>s.ToString()).ToArray(),ghost.HasBadges,ghost.Badge,ghost.SkinCustomization,
                    source_ghost_metadata_version=chunk.Version,source_ghost_record_selection=chunk.U02},events,
                caveat="Raw public GBX.NET notice metadata and payload words only. Unknown payload word meanings and checkpoint/event quantization must be independently corroborated; this output does not approve record-to-race alignment.",
                race_clock_verified=false,video_alignment_verified=false};
            using(var f=new FileStream(output,FileMode.CreateNew))JsonSerializer.Serialize(f,result,new JsonSerializerOptions{WriteIndented=true,PropertyNamingPolicy=JsonNamingPolicy.SnakeCaseLower});
            Console.WriteLine($"Inspected {events.Length} raw clock notices: {output}");return 0;
        }catch(Exception ex){Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
    }
    static string Hash(byte[] b)=>Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();
}
