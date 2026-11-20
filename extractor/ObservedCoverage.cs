using System.Text.Json;

namespace TrackmaniaExtractor;

internal static class ObservedCoverage
{
    internal const string Policy = "observed_10ms_prefix_gap_v1";
    internal record Allowed(string Account, int Finish, int Version, int Ticks, string PackedSha);

    private static JsonDocument? LoadPolicy()
    {
        var path = Environment.GetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY");
        if (string.IsNullOrWhiteSpace(path)) return null;
        var doc = JsonDocument.Parse(File.ReadAllText(path));
        if (doc.RootElement.GetProperty("schema_version").GetInt32() != 1)
            throw new InvalidDataException("Unsupported reviewed coverage policy schema.");
        return doc;
    }

    internal static string MapUid
    {
        get { using var doc = LoadPolicy(); return doc?.RootElement.GetProperty("map_uid").GetString() ?? ""; }
    }

    internal static Allowed[] Sources
    {
        get
        {
            using var doc = LoadPolicy();
            if (doc is null || !doc.RootElement.TryGetProperty("cases", out var cases)) return [];
            return cases.EnumerateObject().Select(item =>
            {
                var value = item.Value;
                return new Allowed(value.GetProperty("account_id").GetString()!,
                    value.GetProperty("record_time_ms").GetInt32(), value.GetProperty("input_version").GetInt32(),
                    value.GetProperty("ticks").GetInt32(), value.GetProperty("packed_sha256").GetString()!);
            }).ToArray();
        }
    }

    public static int KnownRaceStart(Guid? account, string mapUid, int finish, int version,
        int offset, int ticks, string packedSha, StrictResult parsed)
    {
        int end = checked(offset + ticks * 10);
        if (parsed.Rows.Count != ticks || parsed.Rows[0].TimestampMs != offset || parsed.Rows[^1].TimestampMs + 10 != end)
            throw new InvalidDataException("Parsed input interval differs from its original store.");
        if (end < finish) throw new InvalidDataException("Recorded controls end before finish; no extrapolation is permitted.");
        if (offset <= 0) return 0;
        bool allowed = offset == 10 && mapUid == MapUid && Sources.Any(s => account == Guid.Parse(s.Account)
            && finish == s.Finish && version == s.Version && ticks == s.Ticks && packedSha == s.PackedSha);
        if (!allowed) throw new InvalidDataException("Missing race prefix needs an independently reviewed, source-bound coverage policy. No controls are synthesized.");
        var first = parsed.Changes.FirstOrDefault(c => c.Tick == 0);
        if (first is null || first.StateForm != "full" || first.StateBits != 2 || first.InputKind != 2
            || first.SteerRaw is null || first.Throttle is null || first.Brake is null)
            throw new InvalidDataException("First observed tick lacks a complete explicit vehicle-control update.");
        return 10;
    }

    public static object Describe(int finish, int originalEnd) => new
    {
        schema_version = 1, policy = Policy, race_interval_ms = new[] { 0, finish },
        known_race_interval_ms = new[] { 10, finish }, original_input_interval_ms = new[] { 10, originalEnd },
        missing_race_intervals_ms = new[] { new[] { 0, 10 } }, fully_covered = false, extrapolated = false
    };
}
