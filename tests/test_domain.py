import json
from pathlib import Path
import unittest

from collector.domain import *


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(Path('fixtures/sample.json').read_text())

    def test_domain_behavior(self):
        self.assertEqual(collect(self.payload)[0]['title'], '深海观测成果')
        with self.assertRaises(ValueError):
            parse_page('https://lab.example/1', '<h1>空页</h1>')

    def test_deterministic_observations(self):
        self.assertEqual(collect(self.payload), collect(self.payload))
