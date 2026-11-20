using System.Globalization;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;
using GBX.NET.Inputs;
using GBX.NET.LZO;
using GBX.NET.ZLib;
using TrackmaniaExtractor;
using TmEssentials;

internal static class Program
{
    private static readonly JsonSerializerOptions Json = new()
    {
        WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    private static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test") return SelfTest.Run();
        if (args.Length == 0 || args.Contains("--help"))
        {
            Console.WriteLine("TrackmaniaExtractor INPUT.Ghost.Gbx|INPUT.Replay.Gbx --output DIR [--ghost-index N] [--expected-map-uid UID] [--expected-time-ms N] [--expected-login LOGIN] [--expected-account-id UUID]\nOutput must not already exist. Exit 0: extracted; exit 2: rejected (metadata.json explains).\n--self-test exercises framing, offsets, normalization, pulse actions, and rejection cases.");
            return args.Length == 0 ? 2 : 0;
        }
        string? output = null;
        var metadata = new Dictionary<string, object?>
        {
            ["schema_version"] = 1, ["status"] = "rejected", ["extractor_version"] = "0.1.3",
            ["gbx_net_version"] = "2.5.1", ["gbx_net_source_commit"] = "8092a3a6dc192eb7ce3aa56093096f6fe3ac884b",
            ["created_utc"] = DateTimeOffset.UtcNow, ["tick_period_ms"] = 10
        };
        try
        {
            var options = ParseOptions(args);
            output = Path.GetFullPath(options["--output"]);
            if (Directory.Exists(output) || File.Exists(output))
            {
                output = null; // Never overwrite an existing completed or rejected job.
                throw new InvalidDataException("Output already exists; choose a new directory.");
            }
            Directory.CreateDirectory(output);
            string source = Path.GetFullPath(args[0]);
            var bytes = File.ReadAllBytes(source);
            if (bytes.Length > 128 * 1024 * 1024) throw new InvalidDataException("Source exceeds 128 MiB limit.");
            metadata["source_file"] = source;
            metadata["source_bytes"] = bytes.Length;
            metadata["source_sha256"] = Sha(bytes);
            Gbx.LZO = new Lzo();
            Gbx.ZLib = new ZLib();
            var parsed = Gbx.Parse(source, new GbxReadSettings
            {
                IgnoreExceptionsInBody = false, SafeSkippableChunks = false,
                MaxDataSize = 128 * 1024 * 1024, MaxUncompressedBodySize = 128 * 1024 * 1024,
                MaxCompressedBodySize = 128 * 1024 * 1024, MaxSkippableChunkSize = 64 * 1024 * 1024
            });
            CGameCtnReplayRecord? replay = parsed.Node as CGameCtnReplayRecord;
            CGameCtnGhost ghost;
            int ghostIndex = options.TryGetValue("--ghost-index", out var index) ? int.Parse(index, CultureInfo.InvariantCulture) : 0;
            if (parsed.Node is CGameCtnGhost single)
            {
                if (ghostIndex != 0) throw new InvalidDataException("A Ghost.Gbx contains only ghost index 0.");
                ghost = single;
                metadata["ghost_count"] = 1;
            }
            else if (replay is not null)
            {
                var ghosts = replay.GetGhosts().Distinct(ReferenceEqualityComparer.Instance).Cast<CGameCtnGhost>().ToArray();
                metadata["ghost_count"] = ghosts.Length;
                if (ghosts.Length == 0) throw new InvalidDataException("Replay contains no ghosts.");
                if (ghosts.Length != 1 && !options.ContainsKey("--ghost-index"))
                    throw new InvalidDataException($"Replay contains {ghosts.Length} ghosts; specify --ghost-index explicitly.");
                if (ghostIndex < 0 || ghostIndex >= ghosts.Length) throw new InvalidDataException("Ghost index outside replay range.");
                ghost = ghosts[ghostIndex];
            }
            else throw new InvalidDataException($"Unsupported GBX class {parsed.Node?.GetType().Name ?? "unknown"}.");
            metadata["container_class"] = parsed.Node!.GetType().Name;
            metadata["ghost_index"] = ghostIndex;
            metadata["detected_game_version"] = ghost.GameVersion.ToString();
            metadata["map_uid"] = ghost.Validate_ChallengeUid?.ToString();
            metadata["replay_map_uid"] = replay?.MapInfo?.Id.ToString();
            metadata["finish_time_ms"] = ghost.RaceTime?.TotalMilliseconds;
            metadata["replay_finish_time_ms"] = replay?.Time?.TotalMilliseconds;
            metadata["ghost_login"] = ghost.GhostLogin;
            metadata["ghost_nickname"] = ghost.GhostNickname;
            metadata["ghost_uid"] = ghost.GhostUid?.ToString();
            var account = TryAccountId(ghost.GhostLogin);
            metadata["account_id"] = account?.ToString();
            metadata["account_id_note"] = "Decoded with TmEssentials2.8.0 AccountUtils and canonical round-trip validation; cross-check with source leaderboard accountId.";
            metadata["recording_context"] = ghost.RecordingContext;
            metadata["player_model"] = ghost.PlayerModel?.ToString();
            metadata["recorded_respawns"] = ghost.Respawns;
            metadata["checkpoints"] = ghost.Checkpoints?.Select(c => new { time_ms = c.Time?.TotalMilliseconds, c.CheckpointId }).ToArray();
            metadata["game_build"] = new
            {
                ghost.Validate_ExeVersion, ghost.Validate_ExeChecksum, ghost.GhostVersion,
                ghost.Validate_TitleId, ghost.Validate_GameRules,
                race_start_time_ms = ghost.RaceStartTime?.TotalMilliseconds,
                validate_race_start_time_ms = ghost.Validate_RaceStartTime?.TotalMilliseconds,
                ghost.WalltimeStartTimestamp, ghost.WalltimeEndTimestamp
            };
            metadata["chunks"] = ghost.Chunks.Select(c => new
            {
                id = $"0x{c.Id:X8}", version = c.GetType().GetProperty("Version")?.GetValue(c)
            }).ToArray();
            var stores = ghost.PlayerInputs?.ToArray() ?? [];
            metadata["input_stores"] = stores.Select((s, i) => new
            {
                store_index = i, input_version = (int)s.Version, start_time_ms = s.StartTime.TotalMilliseconds,
                start_offset_ms = s.StartOffset?.TotalMilliseconds, ticks = s.Ticks,
                data_bytes = s.Data?.Length ?? 0, data_sha256 = s.Data is null ? null : Sha(s.Data),
                packed_file = $"input_store_{i:D3}.bin"
            }).ToArray();
            for (int i = 0; i < stores.Length; i++)
                if (stores[i].Data is { } raw) File.WriteAllBytes(Path.Combine(output, $"input_store_{i:D3}.bin"), raw);
            metadata["visual_sample_data"] = VisualSamples.Export(ghost, output);
            if (!ghost.CanBeGameVersion(GameVersion.TM2020)) throw new InvalidDataException("Ghost is not identifiable as Trackmania 2020.");
            if (stores.Length != 1)
                throw new InvalidDataException($"Expected exactly one input store; found {stores.Length}. Multi-store joins are unverified and are never silently concatenated.");
            if (ghost.RaceTime is null || ghost.RaceTime.Value.TotalMilliseconds <= 0)
                throw new InvalidDataException("Ghost has no positive finish time.");
            int finish = ghost.RaceTime.Value.TotalMilliseconds;
            string mapUid = ghost.Validate_ChallengeUid?.ToString() ?? "";
            if (string.IsNullOrWhiteSpace(mapUid)) throw new InvalidDataException("Ghost has no validation map UID.");
            if (options.TryGetValue("--expected-map-uid", out var wantedMap) && wantedMap != mapUid)
                throw new InvalidDataException($"Map UID mismatch: expected {wantedMap}, got {mapUid}.");
            if (options.TryGetValue("--expected-time-ms", out var wantedTime) && int.Parse(wantedTime, CultureInfo.InvariantCulture) != finish)
                throw new InvalidDataException($"Finish time mismatch: expected {wantedTime}, got {finish}.");
            if (options.TryGetValue("--expected-login", out var wantedLogin) && wantedLogin != ghost.GhostLogin)
                throw new InvalidDataException("Ghost login differs from expected login.");
            if (options.TryGetValue("--expected-account-id", out var wantedAccount) && (!Guid.TryParse(wantedAccount, out var expectedAccount) || account != expectedAccount))
                throw new InvalidDataException("Ghost account UUID differs from expected accountId or cannot be decoded.");
            if (replay?.MapInfo is { } mapInfo && mapInfo.Id.ToString() != mapUid)
                throw new InvalidDataException("Replay and selected ghost map UIDs disagree.");

            var store = stores[0];
            var strict = StrictVehicleInputs.Read(store, finish);
            var events = store.Inputs.ToArray(); // Time already includes StartOffset. Do not add it again.
            if (events.Length == 0) throw new InvalidDataException("No decoded action events; unsupported/empty input store.");
            var rows = BuildRowsAndCrossCheck(store, events, strict.Rows);
            int knownRaceStart = ObservedCoverage.KnownRaceStart(account, mapUid, finish, (int)store.Version,
                store.StartOffset!.Value.TotalMilliseconds, store.Ticks, Sha(store.Data!), strict);
            if (knownRaceStart > 0)
            {
                metadata["action_coverage"] = ObservedCoverage.Describe(finish, rows[^1].TimestampMs + 10);
                string policyPath = Path.GetFullPath(Environment.GetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY")!);
                byte[] policyBytes = File.ReadAllBytes(policyPath);
                metadata["reviewed_coverage_policy"] = new { path = policyPath, sha256 = Sha(policyBytes), bytes = policyBytes.Length };
            }
            var race = new List<ActionRow>();
            for (int time = knownRaceStart; time < finish; time += 10)
            {
                int tick = (time - rows[0].TimestampMs) / 10;
                var row = rows[tick];
                if (row.InputKind is not (2 or 3 or 4)) throw new InvalidDataException($"Race-time input kind {row.InputKind} is not vehicle at {time} ms.");
                // Sampling pulses on a shifted time grid would lose/duplicate events. Keep raw pulse event time,
                // and mark pulse if it falls in [time,time+10) for the resampled race action interval.
                int pulseEnd = Math.Min(time + 10, finish);
                int respawn = events.Any(e => e is RespawnTM2020 && e.Time.TotalMilliseconds >= time && e.Time.TotalMilliseconds < pulseEnd) ? 1 : 0;
                int secondary = events.Any(e => e is SecondaryRespawn && e.Time.TotalMilliseconds >= time && e.Time.TotalMilliseconds < pulseEnd) ? 1 : 0;
                race.Add(row with { TimestampMs = time, Respawn = respawn, SecondaryRespawn = secondary });
            }
            WriteCsv(Path.Combine(output, "actions_all_100hz.csv"), rows, null);
            WriteCsv(Path.Combine(output, "actions_race_100hz.csv"), race, finish);
            WriteJson(Path.Combine(output, "input_events.json"), events.Select((e, i) => EventRecord(e, i, store)).ToArray());
            WriteJson(Path.Combine(output, "input_changes.json"), strict.Changes);
            metadata["status"] = "extracted";
            metadata["checks"] = new
            {
                strict_bitstream = true, gbx_semantics_match_strict_rows = true,
                single_store = true, race_coverage = knownRaceStart == 0, observed_race_coverage = true, normalized_steering_range = true,
                expected_map_uid_checked = wantedMap is not null,
                expected_finish_time_checked = wantedTime is not null,
                expected_login_checked = wantedLogin is not null,
                expected_account_id_checked = wantedAccount is not null,
                video_alignment_verified = false
            };
            metadata["consumed_input_bits"] = strict.ConsumedBits;
            metadata["zero_padding_bits"] = strict.PaddingBits;
            metadata["input_kinds"] = strict.InputKinds;
            metadata["non_vehicle_boundary_ticks"] = strict.BoundaryTicks;
            metadata["non_vehicle_boundary_policy"] = "Kinds 0xE/0xF are framing evidence only, never vehicle labels. All race intervals require kinds 2/3/4. The all-ticks CSV carries prior vehicle state for provenance; rows with other input_kind values are invalid as vehicle training labels. Original bits, all flags (including unknown bit32), mouse, value marker and opaque payload are preserved. Only initial payload-free 0xE and outside-race absent/zero-eight-bit 0xF with independently verified flag patterns are supported; no gameplay interpretation is inferred.";
            metadata["initial_payload_free_kind_14"] = strict.InputKinds.Contains(14) ? new
            {
                tick = 0, timestamp_ms = rows[0].TimestampMs, raw_full_state = 14,
                same_kind_specific_values_bit = 1, kind_specific_payload_bits = 0,
                next_tick_input_kind = rows[1].InputKind,
                interpretation = "Only this empty initial pre-race v12 state is supported. Raw bytes/state are preserved; no character, vehicle or float-steering payload interpretation is inferred. Race controls still require ordinary vehicle kinds."
            } : null;
            metadata["all_ticks"] = rows.Count;
            metadata["race_ticks"] = race.Count;
            metadata["first_input_timestamp_ms"] = rows[0].TimestampMs;
            metadata["last_input_timestamp_ms"] = rows[^1].TimestampMs;
            metadata["input_event_counts"] = events.GroupBy(e => e.GetType().Name).ToDictionary(g => g.Key, g => g.Count());
            metadata["time_semantics"] = new
            {
                race_timestamp = "GBX.NET Inputs.Time includes StartOffset exactly once; race zero is not StartTime.",
                start_time = "Original replay/simulation clock value, preserved but not added to race-relative events.",
                controls = "Held controls apply from their timestamp until next change; initial state is neutral at first store tick.",
                actions_race_grid = "Only observed race times, on the 10ms grid strictly below finish; controls sampled from last tick at/before grid time. Complete sources start0; the two pinned missing-prefix sources start10 and explicitly leave [0,10) unknown. valid_until_ms clips final interval to finish. No invented initial row or held extrapolation.",
                pulse_controls = "Respawn fields denote events in the row interval, not a held button; exact timestamp remains in input_events.json.",
                action_slots = "Raw slot flags 0..9; no inferred sensitivity/steering transformation.",
                observations = "Video time mapping and the original driver's camera cannot be inferred from actions. Verify native render alignment separately."
            };
            metadata["files"] = Directory.EnumerateFiles(output).Order().ToDictionary(p => Path.GetFileName(p)!, p => (object)new { bytes = new FileInfo(p).Length, sha256 = Sha(File.ReadAllBytes(p)) });
            WriteJson(Path.Combine(output, "metadata.json"), metadata);
            Console.WriteLine(JsonSerializer.Serialize(new { status = "extracted", output, map_uid = mapUid, finish_time_ms = finish, rows = race.Count }));
            return 0;
        }
        catch (Exception ex)
        {
            metadata["status"] = "rejected";
            metadata["error_type"] = ex.GetType().Name;
            metadata["error"] = ex.Message;
            if (output is not null) WriteJson(Path.Combine(output, "metadata.json"), metadata);
            Console.Error.WriteLine(JsonSerializer.Serialize(new { status = "rejected", output, error = ex.Message }));
            return 2;
        }
    }

    private static Dictionary<string, string> ParseOptions(string[] args)
    {
        string[] names = ["--output", "--ghost-index", "--expected-map-uid", "--expected-time-ms", "--expected-login", "--expected-account-id"];
        var result = new Dictionary<string, string>();
        for (int i = 1; i < args.Length; i += 2)
        {
            if (!names.Contains(args[i]) || i + 1 >= args.Length || !result.TryAdd(args[i], args[i + 1]))
                throw new ArgumentException($"Invalid/duplicate option {args[i]}.");
        }
        if (!result.ContainsKey("--output")) throw new ArgumentException("--output is required.");
        return result;
    }

    internal static Guid? TryAccountId(string? login)
    {
        if (Guid.TryParse(login, out var direct)) return direct;
        if (login is null || login.Length != 22 || login.Any(c => !char.IsAsciiLetterOrDigit(c) && c != '-' && c != '_')) return null;
        try
        {
            var account = AccountUtils.ToAccountId(login);
            return AccountUtils.ToLogin(account) == login ? account : null;
        }
        catch (FormatException) { return null; }
    }

    internal static List<ActionRow> BuildRowsAndCrossCheck(CGameCtnGhost.PlayerInputData store, IInput[] events, List<ActionRow> strict)
    {
        int offset = store.StartOffset!.Value.TotalMilliseconds;
        int cursor = 0, steer = 0, gas = 0, brake = 0, horn = 0, freeLook = 0, jump = 0, slots = 0;
        var result = new List<ActionRow>();
        int previousTime = int.MinValue;
        foreach (var e in events)
        {
            int t = e.Time.TotalMilliseconds;
            if (t < previousTime || t < offset || t >= offset + store.Ticks * 10 || (t - offset) % 10 != 0)
                throw new InvalidDataException("Decoded events are unordered, off-grid, or outside their input store.");
            previousTime = t;
        }
        for (int tick = 0; tick < store.Ticks; tick++)
        {
            int time = offset + tick * 10, respawn = 0, secondary = 0;
            while (cursor < events.Length && events[cursor].Time.TotalMilliseconds <= time)
            {
                switch (events[cursor++])
                {
                    case SteerTM2020 e: steer = e.Value; break;
                    case Accelerate e: gas = e.Pressed ? 1 : 0; break;
                    case Brake e: brake = e.Pressed ? 1 : 0; break;
                    case Horn e: horn = e.Pressed ? 1 : 0; break;
                    case FreeLook e: freeLook = e.Pressed ? 1 : 0; break;
                    case Jump e: jump = e.Pressed ? 1 : 0; break;
                    case ActionSlot e:
                        if (e.Slot > 9) throw new InvalidDataException("Unsupported action slot.");
                        slots = e.Pressed ? slots | (1 << e.Slot) : slots & ~(1 << e.Slot); break;
                    case RespawnTM2020: respawn = 1; break;
                    case SecondaryRespawn: secondary = 1; break;
                    case MouseAccu: break;
                    default: throw new InvalidDataException($"Unsupported decoded input type {events[cursor - 1].GetType().Name}.");
                }
            }
            var row = new ActionRow(tick, time, strict[tick].InputKind, steer, gas, brake, horn, freeLook, jump, slots, respawn, secondary);
            if (row != strict[tick]) throw new InvalidDataException($"GBX.NET/strict decoder semantic disagreement at tick {tick}.");
            result.Add(row);
        }
        return result;
    }

    private static object EventRecord(IInput e, int sequence, CGameCtnGhost.PlayerInputData store)
    {
        var properties = e.GetType().GetProperties(BindingFlags.Instance | BindingFlags.Public)
            .Where(p => p.Name != "Time" && p.GetIndexParameters().Length == 0)
            .ToDictionary(p => JsonNamingPolicy.SnakeCaseLower.ConvertName(p.Name), p => p.GetValue(e));
        return new { sequence, store_index = 0, tick = (e.Time.TotalMilliseconds - store.StartOffset!.Value.TotalMilliseconds) / 10,
            timestamp_ms = e.Time.TotalMilliseconds, type = e.GetType().Name, values = properties };
    }

    private static void WriteCsv(string path, IEnumerable<ActionRow> rows, int? finish)
    {
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.WriteLine("store_index,tick,timestamp_ms,valid_until_ms,input_kind,steer_raw,steer_normalized,throttle,brake,horn,free_look,jump,action_slot_mask,respawn,secondary_respawn");
        foreach (var r in rows)
        {
            int end = Math.Min(r.TimestampMs + 10, finish ?? int.MaxValue);
            writer.WriteLine(FormattableString.Invariant($"0,{r.Tick},{r.TimestampMs},{end},{r.InputKind},{r.SteerRaw},{r.SteerRaw / 127.0:R},{r.Throttle},{r.Brake},{r.Horn},{r.FreeLook},{r.Jump},{r.ActionSlotMask},{r.Respawn},{r.SecondaryRespawn}"));
        }
    }

    private static string Sha(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
    private static void WriteJson(string path, object value) => File.WriteAllText(path, JsonSerializer.Serialize(value, Json) + "\n");
}
