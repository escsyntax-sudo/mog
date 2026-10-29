import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mog.spec import Spec, SpecError, resolve_response  # noqa: E402
from mog.util import compile_template, join_paths, resolve_ref, status_to_int  # noqa: E402

EXAMPLES = ROOT / "examples"


def load(name: str) -> Spec:
    document = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return Spec(document, source=str(EXAMPLES / name))


class UtilTests(unittest.TestCase):
    def test_join_paths(self):
        self.assertEqual(join_paths("/v1", "/pets"), "/v1/pets")
        self.assertEqual(join_paths("", "", ""), "/")
        self.assertEqual(join_paths("/a/", "/b/"), "/a/b")

    def test_compile_template(self):
        regex, names = compile_template("/pets/{petId}/toys/{toyId}")
        self.assertEqual(names, ["petId", "toyId"])
        found = regex.match("/pets/9/toys/red-ball")
        self.assertIsNotNone(found)
        self.assertEqual(found.groups(), ("9", "red-ball"))
        self.assertIsNone(regex.match("/pets/9/toys"))

    def test_compile_template_trailing_slash(self):
        regex, _ = compile_template("/pets")
        self.assertIsNotNone(regex.match("/pets"))
        self.assertIsNotNone(regex.match("/pets/"))

    def test_status_to_int(self):
        self.assertEqual(status_to_int("200"), 200)
        self.assertEqual(status_to_int("4XX"), 400)
        self.assertEqual(status_to_int("default"), 200)

    def test_resolve_ref(self):
        document = {"components": {"schemas": {"A/b": {"type": "string"}}}}
        self.assertEqual(resolve_ref(document, "#/components/schemas/A~1b"), {"type": "string"})
        with self.assertRaises(SpecError):
            resolve_ref(document, "#/components/schemas/missing")
        with self.assertRaises(SpecError):
            resolve_ref(document, "other.json#/A")


class PetstoreTests(unittest.TestCase):
    def setUp(self):
        self.spec = load("petstore.json")
        self.pet = self.spec.find("GET", "/v1/pets/42")

    def test_route_count_and_prefix(self):
        self.assertEqual(self.spec.kind, "openapi")
        self.assertEqual(self.spec.version, "3")
        self.assertEqual(len(self.spec.routes), 6)
        self.assertTrue(all(route.template.startswith("/v1/") for route in self.spec.routes))

    def test_static_route_wins_over_parameter_route(self):
        route, params, _ = self.spec.match("GET", "/v1/pets")
        self.assertEqual(route.operation_id, "listPets")
        self.assertEqual(params, {})

    def test_parameter_extraction(self):
        route, params, _ = self.spec.match("GET", "/v1/pets/42")
        self.assertEqual(route.operation_id, "getPet")
        self.assertEqual(params, {"petId": "42"})

    def test_allowed_methods(self):
        route, _, allowed = self.spec.match("PUT", "/v1/pets")
        self.assertIsNone(route)
        self.assertEqual(allowed, ["get", "post"])

    def test_parameters_are_merged(self):
        self.assertIsNotNone(self.pet)
        names = {param.get("name") for param in self.pet.parameters}
        self.assertEqual(names, {"petId"})

    def test_default_response_is_first_success(self):
        route = self.spec.find("POST", "/v1/pets")
        response = resolve_response("3", route)
        self.assertEqual(response.status, 201)
        self.assertTrue(response.has_body)
        self.assertEqual(response.media_type, "application/json")

    def test_delete_response_has_no_body(self):
        route = self.spec.find("DELETE", "/v1/pets/1")
        response = resolve_response("3", route)
        self.assertEqual(response.status, 204)
        self.assertFalse(response.has_body)

    def test_unknown_status_falls_back_to_error_payload(self):
        route = self.spec.find("GET", "/v1/pets")
        response = resolve_response("3", route, 418)
        self.assertEqual(response.status, 418)
        self.assertEqual(response.example["status"], 418)
        self.assertTrue(response.has_example)

    def test_exact_error_status_uses_schema(self):
        route = self.spec.find("GET", "/v1/pets")
        response = resolve_response("3", route, 400)
        self.assertEqual(response.status, 400)
        self.assertIsNotNone(response.schema)
        self.assertFalse(response.has_example)

    def test_response_headers(self):
        route = self.spec.find("GET", "/v1/pets")
        response = resolve_response("3", route)
        self.assertIn("X-Total-Count", response.headers)

    def test_describe(self):
        text = self.spec.describe()
        self.assertIn("petstore.json", text)
        self.assertIn("OpenAPI", text)

    def test_prefix_option(self):
        document = json.loads((EXAMPLES / "petstore.json").read_text(encoding="utf-8"))
        spec = Spec(document, prefix="/mock")
        self.assertTrue(all(route.template.startswith("/mock/v1/") for route in spec.routes))


class SwaggerTests(unittest.TestCase):
    def setUp(self):
        self.spec = load("swagger.json")

    def test_base_path_is_applied(self):
        self.assertEqual(self.spec.version, "2")
        templates = {route.template for route in self.spec.routes}
        self.assertIn("/api/tasks", templates)
        self.assertIn("/api/tasks/{id}", templates)

    def test_produces_and_schema(self):
        route = self.spec.find("GET", "/api/tasks")
        response = resolve_response("2", route)
        self.assertEqual(response.media_type, "application/json")
        self.assertEqual(response.schema["type"], "array")

    def test_body_parameter_is_listed(self):
        route = self.spec.find("POST", "/api/tasks")
        names = {param.get("name") for param in route.parameters}
        self.assertIn("body", names)

    def test_missing_route(self):
        self.assertIsNone(self.spec.find("GET", "/nope"))
        route, _, allowed = self.spec.match("GET", "/nope")
        self.assertIsNone(route)
        self.assertEqual(allowed, [])


class RawPayloadTests(unittest.TestCase):
    def test_payload_becomes_a_route(self):
        payload = [{"id": 1, "name": "beacon"}]
        spec = Spec.from_payload(payload, source="payload.json", path="/events", method="POST", status=201)
        self.assertEqual(spec.kind, "json")
        self.assertEqual(len(spec.routes), 1)
        route = spec.routes[0]
        self.assertEqual(route.template, "/events")
        self.assertEqual(route.method, "post")
        self.assertTrue(route.raw)
        self.assertEqual(route.payload, payload)
        self.assertEqual(list(route.responses.keys()), ["201"])

    def test_payload_route_matches(self):
        spec = Spec.from_payload({"ok": True}, path="/hooks/stripe", method="post")
        route, params, _ = spec.match("POST", "/hooks/stripe")
        self.assertIsNotNone(route)
        self.assertEqual(params, {})
        self.assertEqual(spec.find("POST", "/hooks/stripe").template, "/hooks/stripe")

    def test_payload_prefix(self):
        spec = Spec.from_payload({"ok": True}, path="/hooks", prefix="/dev")
        self.assertEqual(spec.routes[0].template, "/dev/hooks")


class ValidationTests(unittest.TestCase):
    def test_document_without_version_field(self):
        with self.assertRaises(SpecError):
            Spec({"paths": {}})

    def test_document_without_paths(self):
        with self.assertRaises(SpecError):
            Spec({"openapi": "3.0.0", "info": {"title": "x", "version": "1"}})

    def test_non_object_document(self):
        with self.assertRaises(SpecError):
            Spec([1, 2, 3])


if __name__ == "__main__":
    unittest.main()
