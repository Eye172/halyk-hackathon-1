import unittest

from tools.run_sample import run_sample


class PublishedSampleTests(unittest.TestCase):
    def test_single_covenant_replays_with_published_evidence(self):
        result = run_sample()
        self.assertEqual(set(result["answers"]), {"P1"})
        self.assertEqual(set(result["answers"]["P1"]), {"6.1"})
        self.assertEqual(result["answers"]["P1"]["6.1"]["status"], "BREACH")


if __name__ == "__main__":
    unittest.main()
