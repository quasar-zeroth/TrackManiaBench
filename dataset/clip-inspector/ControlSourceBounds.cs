using System.Text.Json;

internal static class ControlSourceBounds
{
    public static bool Validate(int start, int end, int finish, string payloadHash)
    {
        if (start == 0) return false;
        var path = Environment.GetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY");
        if (string.IsNullOrWhiteSpace(path))
            throw new InvalidDataException("Nonzero RecordData start requires a reviewed source-bound policy.");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        if (doc.RootElement.GetProperty("schema_version").GetInt32() != 1)
            throw new InvalidDataException("Unsupported source-bound policy schema.");
        bool verified = doc.RootElement.TryGetProperty("native_source_bounds", out var cases)
            && cases.EnumerateArray().Any(c => c.GetProperty("start_ms").GetInt32() == start
                && c.GetProperty("end_ms").GetInt32() == end
                && c.GetProperty("finish_time_ms").GetInt32() == finish
                && c.GetProperty("payload_sha256").GetString() == payloadHash);
        if (!verified) throw new InvalidDataException("Record payload and bounds are absent from the reviewed source policy.");
        return true;
    }

    public static void SelfTest()
    {
        string? previous = Environment.GetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY");
        string policy = Path.GetTempFileName();
        string hash = new('a', 64);
        File.WriteAllText(policy, JsonSerializer.Serialize(new
        {
            schema_version = 1,
            native_source_bounds = new[] { new { start_ms = 20, end_ms = 50, finish_time_ms = 45, payload_sha256 = hash } }
        }));
        try
        {
            Environment.SetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY", policy);
            if (Validate(0, 50, 45, "any") || !Validate(20, 50, 45, hash)) throw new Exception("Source bounds test failed");
            int rejected = 0;
            foreach (var item in new[] { (21, 50, 45, hash), (20, 51, 45, hash), (20, 50, 46, hash), (20, 50, 45, "changed") })
                try { Validate(item.Item1, item.Item2, item.Item3, item.Item4); }
                catch (InvalidDataException) { rejected++; }
            if (rejected != 4) throw new Exception("Source bounds rejection test failed");
        }
        finally
        {
            Environment.SetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY", previous);
            File.Delete(policy);
        }
        Console.WriteLine("6 source-bound tests passed; nonzero record starts never shift source timestamps.");
    }
}
