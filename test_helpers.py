import os
import tempfile
import unittest
from pathlib import Path

import seven_voice as sv


class HelpersTest(unittest.TestCase):
    def test_parse_ids(self):
        self.assertEqual(sv.parse_ids('1, 2,3'), {1, 2, 3})

    def test_cleanup_transcript(self):
        text = sv.cleanup_transcript('hello comma world question mark laughing emoji')
        self.assertEqual(text, 'hello, world? 😂')

    def test_split_text_respects_limit(self):
        parts = sv.split_text('One. Two. Three.', 7)
        self.assertTrue(all(len(p) <= 7 for p in parts))
        self.assertEqual(parts, ['One.', 'Two.', 'Three.'])

    def test_env_loading(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            try:
                os.chdir(d)
                Path('.env').write_text('DISCORD_TOKEN=t\nAGENT_USER_IDS=10,11\nHUMAN_USER_IDS=12\n')
                for key in ['DISCORD_TOKEN','AGENT_USER_IDS','HUMAN_USER_IDS']:
                    os.environ.pop(key, None)
                cfg = sv.Config.from_env()
                self.assertEqual(cfg.discord_token, 't')
                self.assertEqual(cfg.agent_user_ids, {10, 11})
                self.assertEqual(cfg.human_user_ids, {12})
            finally:
                os.chdir(old)


if __name__ == '__main__':
    unittest.main()
