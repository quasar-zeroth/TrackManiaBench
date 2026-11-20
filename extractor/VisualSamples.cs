using System.Text;
using GBX.NET.Engines.Game;
using GBX.NET.Engines.Scene;

namespace TrackmaniaExtractor;

internal static class VisualSamples
{
    public static object Export(CGameCtnGhost ghost, string output)
    {
        var sources = new List<object>();
        int total = 0;
        using var writer = new StreamWriter(Path.Combine(output, "visual_samples.csv"), false, new UTF8Encoding(false));
        writer.WriteLine("source,entity_index,sample_index,raw_timestamp_ms,position_x,position_y,position_z,rotation_x,rotation_y,rotation_z,rotation_w,velocity_x,velocity_y,velocity_z,speed_kmh");
        if (ghost.RecordData is { } record)
        {
            try
            {
                var entities = new List<object>();
                for (int i = 0; i < record.EntList.Count; i++)
                {
                    var entity = record.EntList[i];
                    var times = new List<int>();
                    foreach (var indexed in entity.Samples.Select((sample, index) => (sample, index)))
                    {
                        if (indexed.sample is not CSceneVehicleVis.EntRecordDelta s || s.Data.Length == 0) continue;
                        var p = s.Position; var q = s.Rotation; var v = s.Velocity;
                        writer.WriteLine(FormattableString.Invariant($"record_data,{i},{indexed.index},{s.Time.TotalMilliseconds},{p.X:R},{p.Y:R},{p.Z:R},{q.X:R},{q.Y:R},{q.Z:R},{q.W:R},{v.X:R},{v.Y:R},{v.Z:R},{s.Speed:R}"));
                        times.Add(s.Time.TotalMilliseconds); total++;
                    }
                    entities.Add(new
                    {
                        entity_index = i, entity.Type, entity.SceneUId,
                        begin_time_ms = entity.BeginTime.TotalMilliseconds, end_time_ms = entity.EndTime.TotalMilliseconds,
                        sample_count = entity.Samples.Count, exported_vehicle_samples = times.Count,
                        first_sample_time_ms = times.Count == 0 ? (int?)null : times[0],
                        last_sample_time_ms = times.Count == 0 ? (int?)null : times[^1],
                        observed_time_steps_ms = times.Zip(times.Skip(1), (a, b) => b - a).Distinct().Order().ToArray()
                    });
                }
                sources.Add(new { source = "record_data", status = "parsed", start_ms = record.Start.TotalMilliseconds,
                    end_ms = record.End.TotalMilliseconds, entities });
            }
            catch (Exception ex)
            {
                sources.Add(new { source = "record_data", status = "unsupported", error = ex.Message });
            }
        }
        if (ghost.RawData is not null || ghost.CompressedData is not null)
        {
            try
            {
                var data = ghost.SampleData;
                var times = new List<int>();
                for (int i = 0; i < data.Samples.Count; i++)
                {
                    var s = data.Samples[i];
                    if (s is not CSceneVehicleCar.Sample) continue;
                    var p = s.Position; var q = s.Rotation; var v = s.Velocity;
                    writer.WriteLine(FormattableString.Invariant($"sample_data,0,{i},{s.Time.TotalMilliseconds},{p.X:R},{p.Y:R},{p.Z:R},{q.X:R},{q.Y:R},{q.Z:R},{q.W:R},{v.X:R},{v.Y:R},{v.Z:R},{s.VelocitySpeed:R}"));
                    times.Add(s.Time.TotalMilliseconds); total++;
                }
                sources.Add(new { source = "sample_data", status = "parsed", data.Version, data.IsFixedTimeStep,
                    sample_period_ms = data.SamplePeriod.TotalMilliseconds,
                    first_sample_time_ms = data.FirstSampleTime?.TotalMilliseconds,
                    data.FirstSampleOffset, data.SavedMobilClassId, sample_count = data.Samples.Count,
                    exported_vehicle_samples = times.Count,
                    observed_time_steps_ms = times.Zip(times.Skip(1), (a, b) => b - a).Distinct().Order().ToArray() });
            }
            catch (Exception ex)
            {
                sources.Add(new { source = "sample_data", status = "unsupported", error = ex.Message });
            }
        }
        return new
        {
            status = total > 0 ? "available_unaligned" : "unavailable",
            exported_samples = total, file = "visual_samples.csv", sources,
            time_semantics = "Raw GBX sample timestamps preserved exactly. No StartOffset or race-start subtraction applied. Establish the sample-to-race-to-render mapping in the visual alignment pilot; sources may use different clocks. Duplicate sources are preserved, never concatenated as one trajectory."
        };
    }
}
