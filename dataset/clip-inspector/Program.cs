using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.Engines.Scene;
using GBX.NET.LZO;
using GBX.NET.ZLib;

if(args.Length==1 && args[0]=="--self-test-timing") return ClipTiming.SelfTest();
if(args.Length>0 && args[0]=="--self-test-map") return SelfTest.Run(args);
if(args.Length>0 && args[0]=="--control-clip") return ControlClip.Run(args);
if(args.Length>0 && args[0]=="--compare-replay") return ReplayCompare.Run(args);
if(args.Length>0 && args[0]=="--inspect-ghost-clocks") return GhostClockInspect.Run(args);
if(args.Length>0 && args[0]=="--rebind-native-ghost") return NativeGhostRebind.Run(args);
if(args.Length>0 && args[0]=="--make-clip") { Console.Error.WriteLine("Rejected: Scratch entities failed actual native renderability. Use --control-clip with an actual exported native scene."); return 2; }
if(args.Length != 2) { Console.Error.WriteLine("Usage: TrackmaniaClipInspector INPUT.Clip.Gbx|INPUT.Replay.Gbx OUTPUT.json (new file only)"); return 2; }
try {
    var src=Path.GetFullPath(args[0]); var dest=Path.GetFullPath(args[1]);
    if(File.Exists(dest)) throw new InvalidDataException("Output exists; refusing overwrite.");
    var bytes=File.ReadAllBytes(src);
    if(bytes.Length>128*1024*1024) throw new InvalidDataException("Clip exceeds 128 MiB limit.");
    Gbx.LZO=new Lzo(); Gbx.ZLib=new ZLib();
    var parsed=Gbx.Parse(src,new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false,MaxDataSize=128*1024*1024,MaxUncompressedBodySize=128*1024*1024});
    var clip = parsed.Node as CGameCtnMediaClip ?? (parsed.Node as CGameCtnReplayRecord)?.Clip;
    if(clip is null) throw new InvalidDataException("Expected Clip.Gbx or Replay.Gbx with a serialized clip (native synthesized clips must be exported first).");
    var tracks=new List<object>(); int entityCount=0;
    for(int ti=0;ti<clip.Tracks.Count;ti++) {
        var track=clip.Tracks[ti]; var blocks=new List<object>();
        for(int bi=0;bi<track.Blocks.Count;bi++) {
            var block=track.Blocks[bi]; var info=new Dictionary<string,object?>{{"block_index",bi},{"class",block.GetType().Name}};
            if(block is CGameCtnMediaBlock.IHasTwoKeys two) {info["start_ms"]=two.Start.TotalMilliseconds;info["end_ms"]=two.End.TotalMilliseconds;}
            if(block is CGameCtnMediaBlock.IHasKeys keyed) info["key_times_ms"]=keyed.Keys.Select(k=>k.Time.TotalMilliseconds).ToArray();
            info["chunks"]=block.Chunks.Select(c=>new{id=$"0x{c.Id:X8}",version=c.GetType().GetProperty("Version")?.GetValue(c)}).ToArray();
            if(block is CGameCtnMediaBlockEntity entity) {
                entityCount++;
                var keys = entity.Keys?.Select(k=>k.Time.TotalMilliseconds).ToArray() ?? [];
                if(keys.Length>0) {
                    if(keys.Zip(keys.Skip(1),(a,b)=>b<a).Any(x=>x)) throw new InvalidDataException("Entity keys out of order.");
                    info["start_ms"]=keys[0]; info["end_ms"]=keys[^1]; info["interval_source"]="serialized_key_bounds";
                } else if(entity.Start is not null && entity.End is not null) {
                    info["interval_source"]="legacy_start_end";
                } else throw new InvalidDataException("Entity has no serialized timing keys or legacy interval.");
                info["legacy_start_ms"]=entity.Start?.TotalMilliseconds;info["legacy_end_ms"]=entity.End?.TotalMilliseconds;
                info["start_offset_ms"]=entity.StartOffset.TotalMilliseconds;info["time_scale"]=entity.TimeScale;
                info["mapping_expression"]="record_ms = start_offset_ms + (clip_ms - start_ms) * time_scale";
                info["player_model"]=entity.PlayerModel.ToString();info["notice_records"]=entity.NoticeRecords;info["appearance_archive_version"]=entity.AppearanceArchiveVersion;info["appearance_color"]=entity.AppearanceColor?.ToString();info["skin_names"]=entity.SkinNames?.Select(s=>s.ToString()).ToArray();info["skin_options"]=entity.SkinOptions;info["has_badges"]=entity.HasBadges;info["no_damage"]=entity.NoDamage;info["ghost_name"]=entity.GhostName;info["race_time_ms"]=entity.RaceTime?.TotalMilliseconds;
                info["entity_render_flags"]=new{entity.ForceTrail,entity.ForceLight,entity.ForceHue,legacy_light_trail_color=entity.LightTrailColor.ToString(),entity.LegacyTrailIntensity};
                info["entity_key_values"]=entity.Keys?.Select(k=>new{time_ms=k.Time.TotalMilliseconds,k.Lights,trail_color=k.TrailColor?.ToString(),k.TrailIntensity,k.SelfIllumIntensity}).ToArray();
                if(entity.Chunks.Get<CGameCtnMediaBlockEntity.Chunk0329F000>() is {} entityChunk)info["entity_unknown_fields"]=new{entityChunk.U06,entityChunk.U07};
                if(entity.RecordData is {} rec) {
                    info["record_data"]=new {start_ms=rec.Start.TotalMilliseconds,end_ms=rec.End.TotalMilliseconds,entities=rec.EntList.Select((e,ei)=>new{
                        entity_index=ei,e.Type,e.SceneUId,begin_time_ms=e.BeginTime.TotalMilliseconds,end_time_ms=e.EndTime.TotalMilliseconds,
                        sample_count=e.Samples.Count,
                        vehicle_samples=e.Samples.Select((s,si)=>(s,si)).Where(x=>x.s is CSceneVehicleVis.EntRecordDelta v && v.Data.Length>0).Select(x=>{
                            var s=(CSceneVehicleVis.EntRecordDelta)x.s;var p=s.Position;var q=s.Rotation;var v=s.Velocity;
                            return new{sample_index=x.si,raw_timestamp_ms=s.Time.TotalMilliseconds,position=new[]{p.X,p.Y,p.Z},rotation=new[]{q.X,q.Y,q.Z,q.W},velocity=new[]{v.X,v.Y,v.Z},speed_kmh=s.Speed};
                        }).ToArray()
                    }).ToArray()};
                }
            }
            if(block is CGameCtnMediaBlockCameraGame camera) {info["game_camera"]=camera.GameCam.ToString();info["game_camera_id"]=(int)camera.GameCam;info["clip_entity_id"]=camera.ClipEntId;info["show_hud"]=camera.ShowHUD;info["show_ping"]=camera.ShowPing;info["show_cross_hair"]=camera.ShowCrossHair;}
            if(block.GetType().GetProperty("Text") is {} text) info["text"]=text.GetValue(block)?.ToString();
            if(block is CGameCtnMediaBlockText textBlock){
                info["text_color"]=new[]{textBlock.Color.X,textBlock.Color.Y,textBlock.Color.Z};
                if(textBlock.Effect is {} effect)info["text_effect"]=new{
                    effect.Centered,effect.ColorBlendMode,effect.IsContinousEffect,effect.IsInterpolated,
                    chunks=effect.Chunks.Select(c=>new{id=$"0x{c.Id:X8}",version=c.GetType().GetProperty("Version")?.GetValue(c)}).ToArray(),
                    keys=effect.Keys?.Select(k=>new{time_ms=k.Time.TotalMilliseconds,position=new[]{k.Position.X,k.Position.Y},
                        rotation=k.Rotation,scale=new[]{k.Scale.X,k.Scale.Y},k.Opacity,k.Depth,k.ColorBlend,color=new[]{k.Color.X,k.Color.Y,k.Color.Z}}).ToArray()};
            }
            blocks.Add(info);
        }
        tracks.Add(new{track_index=ti,track.Name,track.IsKeepPlaying,track.IsCycling,blocks});
    }
    var result=new{schema_version=1,status="clip_inspected_unaligned",container_class=parsed.Node!.GetType().Name,source_path=src,source_sha256=Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(),source_bytes=bytes.Length,
        inspector_version="0.4.1",gbx_net_version="2.5.1",gbx_net_source_commit="8092a3a6dc192eb7ce3aa56093096f6fe3ac884b",clip.Name,clip.LocalPlayerClipEntIndex,entity_blocks=entityCount,tracks,
        race_clock_verified=false,video_alignment_verified=false,time_semantics="Record timestamps and serialized clip mapping are preserved. A measured video-to-clip mapping and independent record-to-race corroboration are required. No default offset is inferred."};
    using(var f=new FileStream(dest,FileMode.CreateNew)) JsonSerializer.Serialize(f,result,new JsonSerializerOptions{WriteIndented=true,PropertyNamingPolicy=JsonNamingPolicy.SnakeCaseLower});
    Console.WriteLine($"Inspected {entityCount} entity blocks: {dest}"); return 0;
} catch(Exception ex) {Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
