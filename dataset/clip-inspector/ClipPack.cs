using System.Security.Cryptography;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.Engines.Control;
using GBX.NET.LZO;
using GBX.NET.ZLib;
using TmEssentials;

internal static class ClipPack
{
    public static int Run(string[] args)
    {
        if(args.Length is not (3 or 4) || (args.Length==4 && args[3]!="--markers")) {Console.Error.WriteLine("Usage: TrackmaniaClipInspector --make-clip INPUT.Ghost.Gbx OUTPUT.Clip.Gbx [--markers]");return 2;}
        try {
            string input=Path.GetFullPath(args[1]),output=Path.GetFullPath(args[2]);
            if(File.Exists(output)||File.Exists(output+".provenance.json")) throw new InvalidDataException("Output already exists.");
            Gbx.LZO=new Lzo();Gbx.ZLib=new ZLib();
            var ghost=Gbx.ParseNode<CGameCtnGhost>(input,new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false});
            if(ghost.RecordData is null || ghost.RaceTime is null || ghost.PlayerModel is null) throw new InvalidDataException("Ghost missing native RecordData, race time or player model.");
            var originalRecordBytes=Serialize(ghost.RecordData);
            // Raw native RecordData is retained; no resimulation, action rewriting or physics conversion.
            var end=TimeSingle.FromMilliseconds(Math.Max(ghost.RecordData.End.TotalMilliseconds,ghost.RaceTime.Value.TotalMilliseconds));
            // Read-only timeline access lazily parses the zlib archive. Restore the unchanged original
            // compressed payload for serialization so no delta re-encoding/recompression occurs.
            if(ghost.RecordData.CompressedData is {} compressed) compressed.Parsed=false;
            var entity=new CGameCtnMediaBlockEntity{RecordData=ghost.RecordData,StartOffset=TimeSingle.Zero,TimeScale=1,
                PlayerModel=ghost.PlayerModel,GhostName=Path.GetFileName(input),RaceTime=ghost.RaceTime,
                Keys=[new(){Time=TimeSingle.Zero},new(){Time=end}]};
            entity.Chunks.Add(new CGameCtnMediaBlockEntity.Chunk0329F000{Version=11});
            entity.Chunks.Add(new CGameCtnMediaBlockEntity.Chunk0329F003());
            var camera=new CGameCtnMediaBlockCameraGame{Start=TimeSingle.Zero,End=end,GameCam=CGameCtnMediaBlockCameraGame.EGameCam.External,
                ClipEntId=1,ShowHUD=false,ShowPing=false,ShowCrossHair=false};
            camera.Chunks.Add(new CGameCtnMediaBlockCameraGame.Chunk03084007{Version=4});
            CGameCtnMediaTrack Track(string name,CGameCtnMediaBlock block) {
                var track=new CGameCtnMediaTrack{Name=name,Blocks=[block]};
                track.Chunks.Add(new CGameCtnMediaTrack.Chunk03078001());track.Chunks.Add(new CGameCtnMediaTrack.Chunk03078005{Version=1});return track;
            }
            bool markers=args.Length==4;
            var markerTimes=markers?new[]{1000,(int)(end.TotalMilliseconds/2/20)*20,(int)((end.TotalMilliseconds-500)/20)*20}:Array.Empty<int>();
            var clip=new CGameCtnMediaClip{Name="Dataset diagnostic clip",LocalPlayerClipEntIndex=0,Tracks=[Track("Original ghost RecordData",entity),Track("External camera",camera)]};
            foreach(int ms in markerTimes) {
                var effect=new CControlEffectSimi{Centered=true,IsInterpolated=false,Keys=[
                    new(){Time=TimeSingle.FromMilliseconds(ms),Position=new Vec2(0,0.7f),Scale=new Vec2(0.1f,0.1f),Opacity=1,Color=new Vec3(1,1,1)},
                    new(){Time=TimeSingle.FromMilliseconds(ms+200),Position=new Vec2(0,0.7f),Scale=new Vec2(0.1f,0.1f),Opacity=1,Color=new Vec3(1,1,1)}]};
                effect.Chunks.Add(new CControlEffectSimi.Chunk07010005());
                var marker=new CGameCtnMediaBlockText{Text=$"$fff$oCLIP {ms} ms",Color=new Vec3(1,1,1),Effect=effect};
                marker.Chunks.Add(new CGameCtnMediaBlockText.Chunk030A8001());marker.Chunks.Add(new CGameCtnMediaBlockText.Chunk030A8002());
                clip.Tracks.Add(Track($"DatasetClockMarker-{ms}",marker));
            }
            clip.Chunks.Add(new CGameCtnMediaClip.Chunk0307900D{Version=1});
            clip.Chunks.Add(new CGameCtnMediaClip.Chunk0307900E{Version=1});
            using(var f=new FileStream(output,FileMode.CreateNew)) new Gbx<CGameCtnMediaClip>(clip).Save(f);
            var roundtrip=Gbx.ParseNode<CGameCtnMediaClip>(output,new GbxReadSettings{IgnoreExceptionsInBody=false,SafeSkippableChunks=false});
            var loaded=roundtrip.Tracks[0].Blocks[0] as CGameCtnMediaBlockEntity ?? throw new InvalidDataException("Missing roundtrip entity.");
            if(loaded.RecordData is null || loaded.TimeScale!=1 || loaded.RaceTime!=ghost.RaceTime || loaded.Keys?.Count!=2 || loaded.Keys[1].Time!=end)
                throw new InvalidDataException("Roundtrip timing mismatch.");
            var srcRec=originalRecordBytes;var outRec=Serialize(loaded.RecordData);
            if(!srcRec.SequenceEqual(outRec)) {
                File.WriteAllBytes(output+".source-record.bin",srcRec);File.WriteAllBytes(output+".roundtrip-record.bin",outRec);
                throw new InvalidDataException("Roundtrip native RecordData bytes changed.");
            }
            string Hash(byte[] b)=>Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();
            var proof=new{schema_version=1,status="diagnostic_clip_roundtrip_verified_native_unverified",source_ghost_path=input,source_ghost_sha256=Hash(File.ReadAllBytes(input)),
                output_clip_path=output,output_clip_sha256=Hash(File.ReadAllBytes(output)),record_data_sha256=Hash(srcRec),record_data_roundtrip_byte_equal=true,
                map_uid=ghost.Validate_ChallengeUid?.ToString(),ghost_login=ghost.GhostLogin,finish_time_ms=ghost.RaceTime.Value.TotalMilliseconds,
                native_import_verified=false,video_alignment_verified=false,diagnostic_markers=markerTimes.Select(ms=>new{start_ms=ms,end_ms=ms+200,text=$"CLIP {ms} ms"}).ToArray(),
                caveat="Supported Clip.Gbx serialization only. Diagnostic clip uses explicit record offset zero and speed one; these are construction values, not measured video/race calibration. Verify native import, car model/appearance, camera target, whole-run coverage and measured alignment before production."};
            using(var f=new FileStream(output+".provenance.json",FileMode.CreateNew)) JsonSerializer.Serialize(f,proof,new JsonSerializerOptions{WriteIndented=true});
            Console.WriteLine($"Created diagnostic Clip.Gbx with byte-equal native RecordData. Native import is unverified: {output}");return 0;
        } catch(Exception ex){Console.Error.WriteLine($"Rejected: {ex.GetType().Name}: {ex.Message}");return 2;}
    }
    private static byte[] Serialize(GBX.NET.Engines.Plug.CPlugEntRecordData rec) {
        var compressed=rec.CompressedData ?? throw new InvalidDataException("Expected native compressed RecordData payload.");
        var chunk=rec.Chunks.Get<GBX.NET.Engines.Plug.CPlugEntRecordData.Chunk0911F000>() ?? throw new InvalidDataException("Missing RecordData version.");
        using var m=new MemoryStream();using var writer=new BinaryWriter(m);
        writer.Write(chunk.Version);writer.Write(compressed.UncompressedSize);writer.Write(compressed.Data);return m.ToArray();
    }
}
