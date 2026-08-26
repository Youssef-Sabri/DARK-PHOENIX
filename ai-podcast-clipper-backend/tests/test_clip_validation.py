import unittest

from clip_validation import parse_clip_moments


class ParseClipMomentsTests(unittest.TestCase):
    def setUp(self):
        self.transcript = [{"start": 0.0, "end": 180.0, "word": "test"}]

    def test_accepts_fenced_json_and_sorts_non_overlapping_clips(self):
        response = """```json
[
  {"start": 90, "end": 130},
  {"start": 0, "end": 45},
  {"start": 40, "end": 80}
]
```"""

        self.assertEqual(
            parse_clip_moments(response, self.transcript),
            [
                {"start": 0.0, "end": 45.0},
                {"start": 90.0, "end": 130.0},
            ],
        )

    def test_rejects_unsafe_or_out_of_range_timestamps(self):
        response = """[
          {"start": "0; rm -rf /", "end": 45},
          {"start": -1, "end": 40},
          {"start": 0, "end": 10},
          {"start": 0, "end": 61},
          {"start": 150, "end": 190}
        ]"""

        self.assertEqual(parse_clip_moments(response, self.transcript), [])

    def test_requires_a_json_list(self):
        with self.assertRaisesRegex(ValueError, "JSON list"):
            parse_clip_moments('{"start": 0, "end": 45}', self.transcript)


if __name__ == "__main__":
    unittest.main()
