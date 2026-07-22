import unittest

from recognition_experiments import parse_flags


class ExperimentFlagsTests(unittest.TestCase):
    def test_parse_comma_and_list_flags(self):
        flags = parse_flags(["temporal_candidate_cache,stable_state_scoring"])
        self.assertTrue(flags.is_enabled("temporal_candidate_cache"))
        self.assertEqual(flags.as_list(), ["stable_state_scoring", "temporal_candidate_cache"])

    def test_rejects_unknown_flags(self):
        with self.assertRaises(ValueError):
            parse_flags(["not_a_real_flag"])


if __name__ == "__main__":
    unittest.main()
