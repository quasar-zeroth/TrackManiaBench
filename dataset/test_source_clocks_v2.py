import base64
import copy
import hashlib
import struct
import unittest
import verify_source_clocks_v2 as c


def example(difference=0, count=3):
    cps = [1000 * (i + 1) for i in range(count)]
    metadata = dict(
        source_sha256="f" * 64,
        map_uid="map",
        ghost_login="login",
        finish_time_ms=cps[-1],
        checkpoints=[dict(time_ms=x) for x in cps],
    )
    events = []
    for index, t in enumerate(cps):
        words = [0, t, t, 0, 0, 65536 if index == len(cps) - 1 else index + 1, 0]
        payload = struct.pack("<7iH", *words, 0)
        events.append(
            dict(
                entity_index=0,
                notice_class_id="0x032CB000",
                notice_type=21,
                notice_archive_version=3,
                raw_payload_base64=base64.b64encode(payload).decode(),
                raw_payload_sha256=hashlib.sha256(payload).hexdigest(),
                raw_payload_bytes=len(payload),
                little_endian_int32_words_for_investigation=words,
                record_timestamp_ms=t + difference,
            )
        )
    raw = {k: v for k, v in metadata.items() if k != "checkpoints"}
    raw.update(checkpoints=[dict(race_time_ms=x) for x in cps], events=events)
    return raw, metadata


class SourceClockTests(unittest.TestCase):
    def test_map_with_more_checkpoints_preserves_every_event(self):
        proof = c.verify(*example(count=5))
        self.assertEqual(proof["corroborated_checkpoint_ordinals"], [1, 2, 3, 4, 5])
        self.assertEqual([p["finish"] for p in proof["checkpoints"]], [False, False, False, False, True])

    def test_too_few_checkpoints_cannot_inherit_three_anchor_policy(self):
        with self.assertRaises(ValueError):
            c.verify(*example(count=2))

    def test_exact_relation(self):
        proof = c.verify(*example())
        self.assertEqual(proof["precision_bound_ms"], 10)
        self.assertEqual(proof["nominal_mapping"]["scale"], 1)

    def test_inclusive_positive_ten(self):
        proof = c.verify(*example(10))
        self.assertEqual(proof["maximum_observed_absolute_difference_ms"], 10)
        self.assertTrue(proof["precision_bound_inclusive"])

    def test_inclusive_negative_ten(self):
        self.assertEqual(c.verify(*example(-10))["maximum_observed_absolute_difference_ms"], 10)

    def test_beyond_supported_bound_rejected(self):
        with self.assertRaises(ValueError):
            c.verify(*example(20))

    def test_missing_finish_rejected(self):
        raw, m = example()
        raw["events"].pop()
        with self.assertRaises(ValueError):
            c.verify(raw, m)

    def test_multiple_exact_entities_rejected(self):
        raw, m = example()
        extra = copy.deepcopy(raw["events"])
        for e in extra:
            e["entity_index"] = 2
        raw["events"] += extra
        with self.assertRaises(ValueError):
            c.verify(raw, m)

    def test_unrelated_entity_does_not_hide_unique_exact_series(self):
        raw, m = example()
        extra = copy.deepcopy(raw["events"][0])
        extra["entity_index"] = 2
        raw["events"].append(extra)
        self.assertEqual(c.verify(raw, m)["checkpoints"][0]["entity_index"], 0)

    def test_payload_hash_tamper(self):
        raw, m = example()
        raw["events"][0]["raw_payload_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            c.verify(raw, m)

    def test_source_identity_tamper(self):
        raw, m = example()
        raw["map_uid"] = "other"
        with self.assertRaises(ValueError):
            c.verify(raw, m)

    def test_multiplayer_arena_self_identity_supported(self):
        raw, m = example()
        raw["entities"] = [dict(entity_index=0, entity_class_id="0x032CB000", scene_uid=247463936)]
        for e in raw["events"]:
            words = e["little_endian_int32_words_for_investigation"]
            words[0] = 247463936
            payload = struct.pack("<7iH", *words, 0)
            e["raw_payload_base64"] = base64.b64encode(payload).decode()
            e["raw_payload_sha256"] = hashlib.sha256(payload).hexdigest()
        self.assertEqual(c.verify(raw, m)["checkpoints"][0]["arena_scene_uid"], 247463936)
        raw["entities"][0]["scene_uid"] += 1
        with self.assertRaises(ValueError):
            c.verify(raw, m)

    def test_no_raw_clock_defaults(self):
        raw, m = example()
        raw["events"] = []
        with self.assertRaises(ValueError):
            c.verify(raw, m)


if __name__ == "__main__":
    unittest.main()
