using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.Engines.Control;
using GBX.NET.LZO;
using GBX.NET.ZLib;
using TmEssentials;

internal static class ControlClip
{
    public static int Run(string[] args)
    {
        if(args.Length<3){Console.Error.WriteLine("Usage: TrackmaniaClipInspector --control-clip NATIVE.Clip.Gbx NEW.Clip.Gbx [--markers] [--preroll-ms N] [--postroll-ms N]");return 2;}
        try {
            bool withMarkers=false;int prerollMs=0,postrollMs=0;var seen=new HashSet<string>();
            for(int i=3;i<args.Length;i++){
                if(!seen.Add(args[i]))throw new InvalidDataException("Duplicate control-clip option.");
                if(args[i]=="--markers")withMarkers=true;
                else if(args[i] is "--preroll-ms" or "--postroll-ms"){
                    string option=args[i];if(++i>=args.Length||!int.TryParse(args[i],out int value))throw new InvalidDataException("Missing/integer timing option required.");
                    if(option=="--preroll-ms")prerollMs=value;else postrollMs=value;
                }else throw new InvalidDataException("Unknown control-clip option: "+args[i]);
            }
            string input=Path.GetFullPath(args[1]),output=Path.GetFullPath(args[2]);
            if(File.Exists(output)||File.Exists(output+".provenance.json"))throw new InvalidDataException("Output already exists.");
            Gbx.LZO=new Lzo();Gbx.ZLib=new ZLib();var settings=new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false};
            var clip=Gbx.ParseNode<CGameCtnMediaClip>(input,settings);
            var entities=clip.Tracks.SelectMany(t=>t.Blocks).OfType<CGameCtnMediaBlockEntity>().ToArray();
            if(entities.Length!=1)throw new InvalidDataException("Expected exactly one native entity.");var entity=entities[0];
            if(entity.RecordData is null||entity.RecordData.CompressedData is null||entity.RaceTime is null||entity.Keys is null||entity.Keys.Count<2)throw new InvalidDataException("Missing native entity timing/RecordData.");
            var originalPayload=entity.RecordData.CompressedData.Data.ToArray();
            int recordEndMs=entity.RecordData.End.TotalMilliseconds;
            bool partialSource=ControlSourceBounds.Validate(entity.RecordData.Start.TotalMilliseconds,recordEndMs,entity.RaceTime.Value.TotalMilliseconds,Hash(originalPayload));
            if(entity.TimeScale!=1||entity.StartOffset.TotalMilliseconds!=0)
                throw new InvalidDataException("Only native offset zero and speed one are supported for this control layout.");
            var layout=ClipTiming.Calculate(recordEndMs,entity.RaceTime.Value.TotalMilliseconds,prerollMs,postrollMs);
            if(withMarkers&&recordEndMs<3000)throw new InvalidDataException("Source is too short for three timing markers.");
            var end=TimeSingle.FromMilliseconds(layout.EntityEndMs);
            entity.RecordData.CompressedData.Parsed=false;
            var originalStart=entity.Keys[0].Time;var originalEnd=entity.Keys[^1].Time;
            foreach(var key in entity.Keys)key.Time=TimeSingle.FromMilliseconds(key.Time.TotalMilliseconds-originalStart.TotalMilliseconds+layout.EntityStartMs);
            entity.Keys[^1].Time=end;
            entity.StartOffset=TimeSingle.Zero;
            // Reuse the complete native entity object: all skins, appearance, notices and private chunk fields survive.
            var ghostTrack=clip.Tracks.Single(t=>t.Blocks.Contains(entity));ghostTrack.Blocks=[entity];
            var camera=new CGameCtnMediaBlockCameraGame{Start=TimeSingle.FromMilliseconds(layout.CameraStartMs),End=end,GameCam=CGameCtnMediaBlockCameraGame.EGameCam.External,
                ClipEntId=1,ShowHUD=false,ShowPing=false,ShowCrossHair=false};
            camera.Chunks.Add(new CGameCtnMediaBlockCameraGame.Chunk03084007{Version=4});
            CGameCtnMediaTrack Track(string name,CGameCtnMediaBlock block){var t=new CGameCtnMediaTrack{Name=name,Blocks=[block]};t.Chunks.Add(new CGameCtnMediaTrack.Chunk03078001());t.Chunks.Add(new CGameCtnMediaTrack.Chunk03078005{Version=1});return t;}
            clip.Tracks=[ghostTrack,Track("Dataset External camera",camera)];
            var markers=withMarkers?layout.MarkerStartsMs:Array.Empty<int>();
            foreach(int ms in markers){
                var effect=new CControlEffectSimi{Centered=true,IsInterpolated=true,Keys=[
                    new(){Time=TimeSingle.FromMilliseconds(ms),Position=new Vec2(0,0),Scale=new Vec2(1,1),Opacity=1,Depth=0.5f,Color=new Vec3(1,1,1)},
                    new(){Time=TimeSingle.FromMilliseconds(ms+200),Position=new Vec2(0,0),Scale=new Vec2(1,1),Opacity=1,Depth=0.5f,Color=new Vec3(1,1,1)}]};
                effect.Chunks.Add(new CControlEffectSimi.Chunk07010005());var text=new CGameCtnMediaBlockText{Text=$"$fff$oCLIP {ms} ms",Color=new Vec3(1,1,1),Effect=effect};
                text.Chunks.Add(new CGameCtnMediaBlockText.Chunk030A8001());text.Chunks.Add(new CGameCtnMediaBlockText.Chunk030A8002());clip.Tracks.Add(Track($"DatasetClockMarker-{ms}",text));
            }
            using(var f=new FileStream(output,FileMode.CreateNew))new Gbx<CGameCtnMediaClip>(clip).Save(f);
            var reread=Gbx.ParseNode<CGameCtnMediaClip>(output,settings);var rereadEntity=reread.Tracks.SelectMany(t=>t.Blocks).OfType<CGameCtnMediaBlockEntity>().Single();
            if(!originalPayload.SequenceEqual(rereadEntity.RecordData!.CompressedData!.Data))throw new InvalidDataException("Native RecordData compressed payload changed.");
            var rereadCamera=reread.Tracks.SelectMany(t=>t.Blocks).OfType<CGameCtnMediaBlockCameraGame>().Single();
            if(rereadEntity.Keys![0].Time.TotalMilliseconds!=layout.EntityStartMs||rereadEntity.Keys[^1].Time.TotalMilliseconds!=layout.EntityEndMs||rereadCamera.Start.TotalMilliseconds!=layout.CameraStartMs||rereadCamera.End.TotalMilliseconds!=layout.CameraEndMs)
                throw new InvalidDataException("Control timing changed during serialization.");
            var proof=new{schema_version=1,preroll_ms=prerollMs,postroll_ms=postrollMs,source_record_end_ms=recordEndMs,
                source_record_start_ms=entity.RecordData.Start.TotalMilliseconds,source_bounds_policy=partialSource?"exact_preserved_positive_record_start_v1":"original_zero_start",
                original_entity_start_ms=originalStart.TotalMilliseconds,camera_start_ms=layout.CameraStartMs,camera_end_ms=layout.CameraEndMs,
                clip_to_record_expression="record_ms = start_offset_ms + (clip_ms - entity_start_ms) * time_scale",
                postroll_hold_verified=false,postroll_note="Entity lifetime extends beyond the final recorded sample; native hold/interpolation/disappearance behavior is unknown until actual native render measurement. Preroll/postfinish frames are excluded from training.",status="native_derived_control_clip_roundtrip_verified_import_unverified",source_clip_path=input,source_clip_sha256=Hash(File.ReadAllBytes(input)),output_clip_path=output,output_clip_sha256=Hash(File.ReadAllBytes(output)),
                original_record_data_payload_sha256=Hash(originalPayload),output_record_data_payload_sha256=Hash(rereadEntity.RecordData.CompressedData.Data),record_data_payload_byte_equal=true,
                original_entity_end_ms=originalEnd.TotalMilliseconds,new_entity_end_ms=end.TotalMilliseconds,entity_start_ms=entity.Keys[0].Time.TotalMilliseconds,
                start_offset_ms=entity.StartOffset.TotalMilliseconds,time_scale=entity.TimeScale,ghost_name=entity.GhostName,finish_time_ms=entity.RaceTime.Value.TotalMilliseconds,
                preserved_complete_native_entity=true,marker_profile="centered_scale1_opacity1_depth0.5_native_visibility_unverified",markers=markers.Select(ms=>new{start_ms=ms,end_ms=ms+200,text=$"CLIP {ms} ms"}).ToArray(),native_import_verified=false,video_alignment_verified=false};
            using(var f=new FileStream(output+".provenance.json",FileMode.CreateNew))JsonSerializer.Serialize(f,proof,new JsonSerializerOptions{WriteIndented=true});
            Console.WriteLine($"Native-derived control clip preserves RecordData payload: {output}");return 0;
        }catch(Exception ex){Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
    }
    private static string Hash(byte[] b)=>Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();
}
