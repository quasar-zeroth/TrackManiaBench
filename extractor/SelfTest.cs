using GBX.NET.Engines.Game;
using GBX.NET.Inputs;
using TmEssentials;

namespace TrackmaniaExtractor;

internal static class SelfTest
{
    public static int Run()
    {
        int checks = 0;
        void Check(bool ok, string name)
        {
            if (!ok) throw new Exception("Self-test failed: " + name);
            checks++;
        }
        var bits = new BitWriter();
        // tick 0: full vehicle state, horn + slot2 + primary respawn; nonzero mouse; full right + gas.
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(2UL | (1UL << 6) | (1UL << 15) | (1UL << 31), 34);
        bits.Put(0, 1); bits.Put(17, 16); bits.Put(29, 16);
        bits.Put(0, 1); bits.Put(127, 8); bits.Put(1, 1); bits.Put(0, 1);
        // tick 1: unchanged flags and vehicle controls; respawn must not remain held.
        bits.Put(1, 1); bits.Put(1, 1); bits.Put(1, 1);
        // tick 2: short horn off state; full left + release gas + brake.
        bits.Put(0, 1); bits.Put(1, 1); bits.Put(0, 2); bits.Put(1, 1);
        bits.Put(0, 1); bits.Put(129, 8); bits.Put(0, 1); bits.Put(1, 1);
        // tick 3: full state clears slot2 and triggers secondary respawn.
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(2UL | (1UL << 33), 34);
        bits.Put(1, 1); bits.Put(1, 1);
        var store = Make(bits.Bytes(), 4, -20);
        var strict = StrictVehicleInputs.Read(store);
        var rows = Program.BuildRowsAndCrossCheck(store, store.Inputs.ToArray(), strict.Rows);
        Check(rows.Select(r => r.TimestampMs).SequenceEqual(new[] { -20, -10, 0, 10 }), "offset applied once");
        Check(rows[0].SteerRaw / 127.0 == 1 && rows[2].SteerRaw / 127.0 == -1, "signed steering normalization");
        Check(rows[0].Respawn == 1 && rows[1].Respawn == 0, "primary respawn pulse");
        Check(rows[3].SecondaryRespawn == 1, "secondary respawn pulse");
        Check(rows[0].ActionSlotMask == 4 && rows[3].ActionSlotMask == 0, "action slot changes");
        Check(rows[1].Horn == 1 && rows[2].Horn == 0, "short horn update preserves other state");
        Check(rows[1].Throttle == 1 && rows[2].Throttle == 0 && rows[2].Brake == 1, "held throttle/brake");
        Check(store.Inputs.OfType<MouseAccu>().Single().X == 17, "raw mouse event preserved");
        Check(Program.TryAccountId("L9H2ZIXkRCmJytSeTSCDoQ") == Guid.Parse("2fd1f664-85e4-4429-89ca-d49e4d2083a1"), "published fixture account identity");
        Check(Program.TryAccountId("not-a-canonical-login") is null, "invalid account encoding rejected");
        void Reject(CGameCtnGhost.PlayerInputData bad, string name)
        {
            try { StrictVehicleInputs.Read(bad); }
            catch (InvalidDataException) { checks++; return; }
            throw new Exception("Self-test accepted " + name);
        }
        Reject(Make(bits.Bytes()[..^1], 4, -20), "truncated packed input");
        var nonzeroTail = bits.Bytes().Concat(new byte[] { 1 }).ToArray();
        Reject(Make(nonzeroTail, 4, -20), "nonzero trailing bits");
        Reject(Make([], 4, -20), "empty input bytes");
        Reject(Make(bits.Bytes(), 0, -20), "empty tick interval");
        var wrongVersion = Make(bits.Bytes(), 4, -20); wrongVersion.Version = (CGameCtnGhost.PlayerInputData.EVersion)13;
        Reject(wrongVersion, "future input version");
        var noOffset = Make(bits.Bytes(), 4, -20); noOffset.StartOffset = null;
        Reject(noOffset, "missing alignment offset");
        var unsupported = new BitWriter(); unsupported.Put(0, 1); unsupported.Put(0, 1); unsupported.Put(12, 34);
        Reject(Make(unsupported.Bytes(), 1, 0), "float steering kind");
        var emptyKind14 = InitialKind14();
        var prefixStore = Make(emptyKind14.Bytes(), 2, -1500);
        var prefixStrict = StrictVehicleInputs.Read(prefixStore);
        var prefixRows = Program.BuildRowsAndCrossCheck(prefixStore, prefixStore.Inputs.ToArray(), prefixStrict.Rows);
        Check(prefixRows[0].InputKind == 14 && prefixRows[0].TimestampMs == -1500
            && prefixRows[0].SteerRaw == 0 && prefixRows[0].Throttle == 0, "empty pre-race record is not steering");
        Check(prefixRows[1].InputKind == 2 && prefixRows[1].SteerRaw == -127 && prefixRows[1].Throttle == 1,
            "next-tick vehicle controls independently match GBX.NET");
        Check(prefixStrict.Changes[0].StateBits == 14 && prefixStrict.Changes[0].SteerRaw is null,
            "initial raw state preserved without invented control update");
        Check(!prefixStore.Inputs.Any(e => e is Strafe or Walk or Vertical or Horizontal or GunTrigger or GBX.NET.Inputs.Action),
            "payload-free prefix has no fabricated character events");
        Reject(Make(InitialKind14(sameValues: false).Bytes(), 2, -1500), "nonempty kind14 payload");
        Reject(Make(InitialKind14().Bytes(), 2, 0), "kind14 at race time");
        Reject(Make(InitialKind14(state: 14UL | (1UL << 6)).Bytes(), 2, -1500), "kind14 additional action flags");
        Reject(Make(InitialKind14(nextKind: 0).Bytes(), 2, -1500), "kind14 not followed by vehicle");
        Reject(Make(InitialKind14(nextKind: 14).Bytes(), 2, -1500), "kind14 persists beyond initial tick");
        var oldPrefix = Make(InitialKind14().Bytes(), 2, -1500);
        oldPrefix.Version = CGameCtnGhost.PlayerInputData.EVersion._2020_04_08;
        Reject(oldPrefix, "unverified v11 kind14 prefix");
        Reject(Make(InitialKind14().Bytes()[..4], 2, -1500), "truncated kind14 framing");
        var boundaryStore = Make(BoundarySequence().Bytes(), 3, -10);
        var boundaryStrict = StrictVehicleInputs.Read(boundaryStore, 10);
        var boundaryRows = Program.BuildRowsAndCrossCheck(boundaryStore, boundaryStore.Inputs.ToArray(), boundaryStrict.Rows);
        Check(boundaryRows.Select(r => r.InputKind).SequenceEqual(new[] {15, 2, 15}), "opaque boundary kinds excluded from race interval");
        Check(boundaryRows[1].SteerRaw == 127 && boundaryRows[1].Throttle == 1, "race controls independently agree after F boundary");
        Check(boundaryStrict.BoundaryTicks[0].HeldFullStateBits == 4294967311UL, "unknown boundary bit32 preserved");
        Check(boundaryStrict.BoundaryTicks[0].MouseX == 123 && boundaryStrict.BoundaryTicks[0].MouseY == 456,
            "boundary mouse payload preserved");
        Check(boundaryStrict.BoundaryTicks[0].KindSpecificPayloadBits == 0
            && boundaryStrict.BoundaryTicks[1].KindSpecificPayloadBits == 8
            && boundaryStrict.BoundaryTicks[1].KindSpecificPayloadRaw == 0, "absent and zero-eight-bit payloads distinguished");
        Check(boundaryStrict.BoundaryTicks[1].BitEnd == boundaryStrict.ConsumedBits, "boundary complete bit extent recorded");
        Check(!boundaryStore.Inputs.Any(e => e is Strafe or Walk or Vertical or Horizontal or GunTrigger or GBX.NET.Inputs.Action),
            "zero boundary values fabricate no nonvehicle actions");
        void RejectBoundary(CGameCtnGhost.PlayerInputData bad, int? finish, string name)
        {
            try { StrictVehicleInputs.Read(bad, finish); }
            catch (InvalidDataException) { checks++; return; }
            throw new Exception("Self-test accepted " + name);
        }
        RejectBoundary(boundaryStore, null, "F without known finish");
        RejectBoundary(boundaryStore, 11, "F overlapping final race interval");
        RejectBoundary(Make(BoundarySequence().Bytes(), 3, -5), 10, "F overlapping race start");
        RejectBoundary(Make(BoundarySequence(payload: 1).Bytes(), 3, -10), 10, "nonzero opaque F payload");
        RejectBoundary(Make(BoundarySequence(flags: 15UL | (1UL << 5)).Bytes(), 3, -10), 10, "unverified F action flags");
        RejectBoundary(Make(BoundarySequence().Bytes()[..^1], 3, -10), 10, "truncated F payload");
        var boundaryV11 = Make(BoundarySequence().Bytes(), 3, -10);
        boundaryV11.Version = CGameCtnGhost.PlayerInputData.EVersion._2020_04_08;
        RejectBoundary(boundaryV11, 10, "unverified F v11 framing");
        string? previousPolicy = Environment.GetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY");
        string temporaryPolicy = Path.GetTempFileName();
        File.WriteAllText(temporaryPolicy, """
            {"schema_version":1,"map_uid":"SyntheticSelfTest","cases":{"synthetic":{"account_id":"11111111-1111-4111-8111-111111111111","record_time_ms":35,"input_version":12,"ticks":3,"packed_sha256":"1111111111111111111111111111111111111111111111111111111111111111"}}}
            """);
        try
        {
            Environment.SetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY", temporaryPolicy);
        foreach(var allowed in ObservedCoverage.Sources)
        {
            var observedRows=Enumerable.Range(0,allowed.Ticks).Select(t=>new ActionRow(t,10+t*10,2,0,1,0,0,0,0,0,0,0)).ToList();
            var observedChanges=new List<RawChange>{new(0,10,"full",2,2,12,34,0,1,0)};
            var observed=new StrictResult(observedRows,observedChanges,80,0,[2],[]);
            int Resolve(StrictResult value,string? sha=null,int? finish=null,Guid? account=null,int? version=null,int offset=10)
                =>ObservedCoverage.KnownRaceStart(account??Guid.Parse(allowed.Account),ObservedCoverage.MapUid,
                    finish??allowed.Finish,version??allowed.Version,offset,allowed.Ticks,sha??allowed.PackedSha,value);
            Check(Resolve(observed)==10,"pinned partial source retains unknown initial interval");
            Check(observed.Rows[0].TimestampMs==10&&observed.Rows[^1].TimestampMs+10>=allowed.Finish,
                "no fabricated initial tick and real suffix coverage");
            void RejectCoverage(Func<int> run,string name){try{run();}catch(InvalidDataException){checks++;return;}throw new Exception("Self-test accepted "+name);}
            RejectCoverage(()=>Resolve(observed,sha:new string('0',64)),"unknown packed source with prefix gap");
            RejectCoverage(()=>Resolve(observed,account:Guid.Empty),"unknown account with prefix gap");
            RejectCoverage(()=>Resolve(observed,finish:allowed.Finish+20),"unrecorded suffix");
            RejectCoverage(()=>Resolve(observed with{Changes=[]}),"first observed state missing explicit controls");
            RejectCoverage(()=>Resolve(observed,version:13),"unsupported prefix store version");
        }
        }
        finally
        {
            Environment.SetEnvironmentVariable("TRACKMANIABENCH_COVERAGE_POLICY", previousPolicy);
            File.Delete(temporaryPolicy);
        }
        Console.WriteLine($"Self-tests passed: {checks}");
        return 0;
    }

    private static BitWriter BoundarySequence(ulong flags = 4294967311UL, ulong payload = 0)
    {
        var bits = new BitWriter();
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(flags, 34);
        bits.Put(0, 1); bits.Put(123, 16); bits.Put(456, 16); bits.Put(1, 1);
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(2, 34); bits.Put(1, 1);
        bits.Put(0, 1); bits.Put(127, 8); bits.Put(1, 1); bits.Put(0, 1);
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(15, 34); bits.Put(1, 1);
        bits.Put(0, 1); bits.Put(payload, 8);
        return bits;
    }

    private static BitWriter InitialKind14(bool sameValues = true, ulong state = 14, ulong nextKind = 2)
    {
        var bits = new BitWriter();
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(state, 34); bits.Put(1, 1);
        bits.Put(sameValues ? 1UL : 0, 1);
        if (!sameValues) bits.Put(0, 8);
        bits.Put(0, 1); bits.Put(0, 1); bits.Put(nextKind, 34); bits.Put(1, 1);
        if (nextKind is 2 or 3 or 4)
        {
            bits.Put(0, 1); bits.Put(129, 8); bits.Put(1, 1); bits.Put(0, 1);
        }
        else if (nextKind == 14) bits.Put(1, 1);
        return bits;
    }

    private static CGameCtnGhost.PlayerInputData Make(byte[] data, int ticks, int offset) => new()
    {
        Version = CGameCtnGhost.PlayerInputData.EVersion._2020_07_20,
        StartTime = new TimeInt32(65430), StartOffset = new TimeInt32(offset), Ticks = ticks, Data = data
    };

    private sealed class BitWriter
    {
        private readonly List<byte> bytes = [];
        private int position;
        public void Put(ulong value, int count)
        {
            for (int bit = 0; bit < count; bit++, position++)
            {
                if (position / 8 == bytes.Count) bytes.Add(0);
                bytes[position / 8] |= (byte)(((value >> bit) & 1) << (position % 8));
            }
        }
        public byte[] Bytes() => bytes.ToArray();
    }
}
