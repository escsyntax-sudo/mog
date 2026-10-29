import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mog.generator import Faker, infer_schema, pattern_sample  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA = {
    "type": "object",
    "required": ["id", "email", "score", "kind"],
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "email": {"type": "string", "format": "email"},
        "score": {"type": "number", "minimum": 10, "maximum": 20},
        "kind": {"type": "string", "enum": ["alpha", "beta"]},
        "note": {"type": "string", "maxLength": 5},
        "bornAt": {"type": "string", "format": "date"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 2,
        },
        "active": {"type": "boolean"},
    },
}


class DeterminismTests(unittest.TestCase):
    def test_same_seed_same_value(self):
        first = Faker(seed=17).generate(SCHEMA)
        second = Faker(seed=17).generate(SCHEMA)
        self.assertEqual(first, second)

    def test_different_seed_different_value(self):
        first = Faker(seed=1).generate(SCHEMA)
        second = Faker(seed=2).generate(SCHEMA)
        self.assertNotEqual(first, second)

    def test_seed_zero_is_stable(self):
        value = Faker().generate(SCHEMA)
        self.assertEqual(value, Faker(seed=0).generate(SCHEMA))


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.value = Faker(seed=9).generate(SCHEMA)

    def test_required_keys_present(self):
        for key in ("id", "email", "score", "kind"):
            self.assertIn(key, self.value)

    def test_formats(self):
        self.assertRegex(self.value["id"], UUID_RE)
        self.assertRegex(self.value["email"], EMAIL_RE)
        self.assertRegex(self.value["bornAt"], DATE_RE)

    def test_enum_membership(self):
        self.assertIn(self.value["kind"], ("alpha", "beta"))

    def test_numeric_range(self):
        self.assertGreaterEqual(self.value["score"], 10)
        self.assertLessEqual(self.value["score"], 20)

    def test_max_length(self):
        self.assertLessEqual(len(self.value["note"]), 5)

    def test_array_bounds(self):
        self.assertEqual(len(self.value["tags"]), 2)

    def test_boolean_type(self):
        self.assertIsInstance(self.value["active"], bool)

    def test_optional_properties_can_be_dropped(self):
        value = Faker(seed=9, include_optional=False).generate(SCHEMA)
        self.assertEqual(set(value.keys()), {"id", "email", "score", "kind"})


class FormatTests(unittest.TestCase):
    def test_formats_and_types(self):
        faker = Faker(seed=4)
        self.assertRegex(faker.generate({"type": "string", "format": "date-time"}), r"T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(faker.generate({"type": "string", "format": "ipv4"}), r"^\d+\.\d+\.\d+\.\d+$")
        self.assertTrue(faker.generate({"type": "string", "format": "uri"}).startswith("https://"))
        self.assertIsInstance(faker.generate({"type": "integer"}), int)
        self.assertIsInstance(faker.generate({"type": "number"}), float)
        self.assertIsInstance(faker.generate({"type": "boolean"}), bool)

    def test_name_hints(self):
        faker = Faker(seed=3)
        self.assertIn("@", faker.generate({"type": "string"}, "email"))
        self.assertRegex(faker.generate({"type": "string"}, "sha256"), r"^[0-9a-f]{64}$")

    def test_pattern(self):
        faker = Faker(seed=6)
        value = faker.generate({"type": "string", "pattern": "^[A-Z]{2}-\\d{4}$"})
        self.assertRegex(value, r"^[A-Z]{2}-\d{4}$")

    def test_pattern_sample_subset(self):
        sample = pattern_sample("[a-c]{4}", Faker().rng)
        self.assertRegex(sample, r"^[a-c]{4}$")
        self.assertIsNone(pattern_sample("(a|b)", Faker().rng))

    def test_min_length_padding(self):
        value = Faker(seed=1).generate({"type": "string", "minLength": 40})
        self.assertGreaterEqual(len(value), 40)


class SchemaFeatureTests(unittest.TestCase):
    def test_const(self):
        self.assertEqual(Faker().generate({"const": {"a": 1}}), {"a": 1})

    def test_example_reuse(self):
        schema = {"type": "object", "example": {"name": "Ada"}}
        self.assertEqual(Faker().generate(schema), {"name": "Ada"})
        generated = Faker(use_examples=False).generate(
            {"type": "object", "properties": {"name": {"type": "string"}}}
        )
        self.assertNotEqual(generated, {"name": "Ada"})

    def test_default_value(self):
        self.assertEqual(Faker().generate({"type": "integer", "default": 7}), 7)

    def test_nullable_probability(self):
        schema = {"type": "string", "nullable": True}
        self.assertIsNone(Faker(seed=2, null_probability=1.0).generate(schema))
        self.assertIsNotNone(Faker(seed=2, null_probability=0.0).generate(schema))

    def test_one_of(self):
        value = Faker(seed=1).generate({"oneOf": [{"type": "string"}, {"type": "integer"}]})
        self.assertIsInstance(value, (str, int))

    def test_all_of_merges_properties(self):
        schema = {
            "allOf": [
                {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"b": {"type": "integer"}}},
            ]
        }
        value = Faker(seed=1).generate(schema)
        self.assertIn("a", value)
        self.assertIn("b", value)

    def test_ref_resolution(self):
        document = {
            "components": {
                "schemas": {"Name": {"type": "string", "format": "uuid"}}
            }
        }
        value = Faker(document=document, seed=1).generate(
            {"$ref": "#/components/schemas/Name"}
        )
        self.assertRegex(value, UUID_RE)

    def test_recursive_schema_terminates(self):
        document = {
            "components": {
                "schemas": {
                    "Node": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "children": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Node"},
                            },
                        },
                    }
                }
            }
        }
        value = Faker(document=document, seed=5, max_depth=3).generate(
            {"$ref": "#/components/schemas/Node"}
        )
        self.assertIn("name", value)

    def test_unresolved_ref_is_harmless(self):
        value = Faker(seed=1).generate({"$ref": "#/components/schemas/Missing"})
        self.assertEqual(value, {})

    def test_integer_bounds_and_multiple(self):
        faker = Faker(seed=8)
        self.assertEqual(
            faker.generate({"type": "integer", "minimum": 5, "maximum": 5}), 5
        )
        value = faker.generate({"type": "integer", "minimum": 10, "maximum": 100, "multipleOf": 5})
        self.assertEqual(value % 5, 0)

    def test_empty_schema(self):
        self.assertEqual(Faker(seed=1).generate({}), "value")
        self.assertEqual(Faker(seed=1).generate(True), "value")


class InferSchemaTests(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(infer_schema(None), {"nullable": True})
        self.assertEqual(infer_schema(True), {"type": "boolean"})
        self.assertEqual(infer_schema(3), {"type": "integer"})
        self.assertEqual(infer_schema(3.5), {"type": "number"})

    def test_string_formats(self):
        self.assertEqual(infer_schema("2026-01-02T03:04:05Z")["format"], "date-time")
        self.assertEqual(infer_schema("2026-01-02")["format"], "date")
        self.assertEqual(infer_schema("a@b.com")["format"], "email")
        self.assertEqual(infer_schema("https://example.com/x")["format"], "uri")
        self.assertEqual(
            infer_schema("3b241101-e2bb-4255-8caf-4136c566a962")["format"], "uuid"
        )

    def test_objects_and_arrays(self):
        schema = infer_schema({"name": "Ada", "tags": ["a", "b"]})
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["name", "tags"])
        self.assertEqual(schema["properties"]["tags"]["type"], "array")
        self.assertEqual(schema["properties"]["tags"]["items"]["type"], "string")

    def test_array_of_objects_merges_keys(self):
        schema = infer_schema([{"a": 1}, {"b": "x"}])
        items = schema["items"]
        self.assertEqual(set(items["properties"].keys()), {"a", "b"})

    def test_empty_array(self):
        self.assertEqual(infer_schema([]), {"type": "array", "items": {}})

    def test_depth_limit(self):
        deep = {"a": {"b": {"c": {"d": 1}}}}
        schema = infer_schema(deep, max_depth=1)
        self.assertEqual(schema["type"], "object")


if __name__ == "__main__":
    unittest.main()
