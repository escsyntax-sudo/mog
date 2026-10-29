import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

from mog.spec import Spec  # noqa: E402
from mog.util import SpecError, parse_document  # noqa: E402

EXAMPLES = ROOT / "examples"
SIMPLE_YAML = """
openapi: 3.0.0
info:
  title: Tiny
  version: 1.0.0
paths: {}
"""


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed")
class YamlTests(unittest.TestCase):
    def test_parse_yaml_document(self):
        document = parse_document(SIMPLE_YAML, "tiny.yaml")
        self.assertEqual(document["openapi"], "3.0.0")

    def test_yaml_example_is_served(self):
        text = (EXAMPLES / "notes.yaml").read_text(encoding="utf-8")
        document = parse_document(text, "notes.yaml")
        spec = Spec(document, source=str(EXAMPLES / "notes.yaml"))
        self.assertEqual(len(spec.routes), 3)
        templates = {route.template for route in spec.routes}
        self.assertIn("/notes/{noteId}", templates)


class YamlDependencyTests(unittest.TestCase):
    @unittest.skipIf(HAVE_YAML, "PyYAML is installed, so the fallback never runs")
    def test_missing_yaml_points_at_the_extra(self):
        with self.assertRaises(SpecError) as caught:
            parse_document("openapi: 3.0.0\ninfo:\n  title: x\n", "x.yaml")
        message = str(caught.exception)
        self.assertIn("mog[yaml]", message)
        self.assertIn("invalid JSON", message)


if __name__ == "__main__":
    unittest.main()
