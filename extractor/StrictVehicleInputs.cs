// Framing based on GBX.NET 2.5.1 PlayerInputData (MIT), commit 8092a3a6dc192eb7ce3aa56093096f6fe3ac884b.
// GBX.NET is the container/parser and authoritative semantic input decoder.
// This stricter pass prevents its deliberate IndexOutOfRange catch from accepting truncation.
using GBX.NET.Engines.Game;

namespace TrackmaniaExtractor;

internal record ActionRow(int Tick, int TimestampMs, int InputKind, int SteerRaw, int Throttle,
    int Brake, int Horn, int FreeLook, int Jump, int ActionSlotMask, int Respawn, int SecondaryRespawn);

internal record RawChange(int Tick, int TimestampMs, string? StateForm, ulong? StateBits,
    int InputKind, int? MouseX, int? MouseY, int? SteerRaw, int? Throttle, int? Brake);

internal record StrictResult(List<ActionRow> Rows, List<RawChange> Changes, int ConsumedBits,
    int PaddingBits, int[] InputKinds, List<BoundaryTick> BoundaryTicks);

// Framing evidence only: these values are deliberately not vehicle action labels.
internal record BoundaryTick(int Tick, int TimestampMs, int InputKind, int BitStart, int BitEnd,
    ulong HeldFullStateBits, string? StateForm, ulong? ChangedStateBits, int? MouseX, int? MouseY,
    int SameKindSpecificValuesBit, int KindSpecificPayloadBits, int? KindSpecificPayloadRaw);

internal static class StrictVehicleInputs
{
    public static StrictResult Read(CGameCtnGhost.PlayerInputData store, int? finishTimeMs = null)
    {
        int version = (int)store.Version;
        if (version is not 11 and not 12)
            throw new InvalidDataException($"Unsupported input version {version}; supported: 11, 12.");
        if (store.StartOffset is null)
            throw new InvalidDataException("Input store has no race-time StartOffset; alignment is unverified.");
        if (store.Ticks is <= 0 or > 120000)
            throw new InvalidDataException($"Invalid/unsupported tick count {store.Ticks}.");
        var data = store.Data ?? throw new InvalidDataException("Input store has no packed data.");
        if (data.Length == 0) throw new InvalidDataException("Input store is empty.");
        int offset = store.StartOffset.Value.TotalMilliseconds;
        var bits = new StrictBits(data);
        var rows = new List<ActionRow>(store.Ticks);
        var changes = new List<RawChange>();
        var kinds = new HashSet<int>();
        var boundaries = new List<BoundaryTick>();
        ulong heldFullState = 0;
        bool initialPayloadFreeKind14 = false;
        int kind = 0, steer = 0, gas = 0, brake = 0, horn = 0, freeLook = 0, jump = 0, slots = 0;
        for (int tick = 0; tick < store.Ticks; tick++)
        {
            int bitStart = bits.Position;
            int time = checked(offset + tick * 10);
            int respawn = 0, secondary = 0;
            string? form = null;
            ulong? rawStates = null;
            int? mouseX = null, mouseY = null, steerUpdate = null, gasUpdate = null, brakeUpdate = null;
            if (bits.Read(1) == 0)
            {
                bool shortForm = bits.Read(1) != 0;
                ulong flags = bits.Read(shortForm ? 2 : version == 11 ? 33 : 34);
                form = shortForm ? "two_bit" : "full";
                rawStates = flags;
                if (shortForm)
                {
                    if (kind == 15) throw new InvalidDataException("Unverified short-form state in kind 0xF boundary.");
                    if (kind is 2 or 3 or 4) horn = (int)((flags >> 1) & 1);
                }
                else
                {
                    heldFullState = flags;
                    kind = (int)(flags & 15);
                    if (kind == 14)
                    {
                        // The observed initial record contains no kind-specific values.
                        // Do not infer a character/vehicle/float payload interpretation.
                        if (version != 12 || tick != 0 || time >= 0 || flags != 14 || store.Ticks < 2)
                            throw new InvalidDataException("Kind 0xE is supported only as an otherwise empty initial pre-race v12 full state.");
                        initialPayloadFreeKind14 = true;
                    }
                    else if (kind == 15)
                    {
                        // Upstream frames this as Character, but its gameplay meaning is unproven.
                        // Only the independently observed flag patterns and zero/absent payload
                        // are accepted, outside every race-control interval.
                        if (version != 12 || flags is not (15UL or 4294967311UL))
                            throw new InvalidDataException("Unsupported kind 0xF boundary flags/version.");
                    }
                    else if (kind is not (0 or 2 or 3 or 4))
                        throw new InvalidDataException($"Unsupported input kind 0x{kind:X} at tick {tick}; no payload interpretation is inferred.");
                    if (kind is 2 or 3 or 4) horn = (int)((flags >> 6) & 1);
                    jump = (int)((flags >> 12) & 1);
                    freeLook = (int)((flags >> 13) & 1);
                    slots = (int)((flags >> 23) & 1);
                    for (int slot = 1; slot <= 9; slot++) slots |= (int)((flags >> (13 + slot)) & 1) << slot;
                    respawn = (int)((flags >> 31) & 1);
                    secondary = (int)((flags >> 33) & 1);
                }
            }
            if (initialPayloadFreeKind14 && tick == 1 && kind is not (2 or 3 or 4))
                throw new InvalidDataException("Empty initial kind 0xE must transition to ordinary vehicle controls on the next tick.");
            if (kind == 15 && (finishTimeMs is null or <= 0 || (time < finishTimeMs && time + 10 > 0)))
                throw new InvalidDataException("Kind 0xF needs an explicit finish time and must not overlap [0, finish).");
            if (bits.Read(1) == 0)
            {
                mouseX = (int)bits.Read(16);
                mouseY = (int)bits.Read(16);
            }
            if (kind is 2 or 3 or 4 && bits.Read(1) == 0)
            {
                int raw = (int)bits.Read(8);
                steer = raw < 128 ? raw : raw - 256;
                if (steer is < -127 or > 127)
                    throw new InvalidDataException($"Steering {steer} is outside normalized range at tick {tick}.");
                gas = (int)bits.Read(1);
                brake = (int)bits.Read(1);
                steerUpdate = steer; gasUpdate = gas; brakeUpdate = brake;
            }
            if (kind is 14 or 15)
            {
                int sameValues = (int)bits.Read(1), payloadBits = 0;
                int? payload = null;
                if (sameValues == 0)
                {
                    if (kind == 14)
                        throw new InvalidDataException("Kind 0xE contains an unsupported nonempty payload; it is never decoded as steering or discarded.");
                    payloadBits = 8; // Exact four-two-bit-field framing from pinned upstream.
                    payload = (int)bits.Read(payloadBits);
                    if (payload != 0) throw new InvalidDataException("Nonzero kind 0xF boundary payload is unsupported.");
                }
                boundaries.Add(new(tick, time, kind, bitStart, bits.Position, heldFullState,
                    form, rawStates, mouseX, mouseY, sameValues, payloadBits, payload));
            }
            kinds.Add(kind);
            rows.Add(new(tick, time, kind, steer, gas, brake, horn, freeLook, jump, slots, respawn, secondary));
            if (form is not null || mouseX is not null || steerUpdate is not null)
                changes.Add(new(tick, time, form, rawStates, kind, mouseX, mouseY, steerUpdate, gasUpdate, brakeUpdate));
        }
        int consumed = bits.Position;
        while (bits.Position < data.Length * 8)
            if (bits.Read(1) != 0) throw new InvalidDataException("Packed input has unexplained nonzero trailing bits.");
        return new(rows, changes, consumed, data.Length * 8 - consumed, kinds.Order().ToArray(), boundaries);
    }

    private sealed class StrictBits(byte[] data)
    {
        public int Position { get; private set; }
        public ulong Read(int count)
        {
            if (Position + count > data.Length * 8)
                throw new InvalidDataException($"Truncated input bitstream: bit {Position}, requested {count}, length {data.Length * 8}.");
            ulong value = 0;
            for (int i = 0; i < count; i++, Position++)
                value |= (ulong)((data[Position / 8] >> (Position % 8)) & 1) << i;
            return value;
        }
    }
}
