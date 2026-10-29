import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mog.server import ServerOptions, create_server  # noqa: E402
from mog.spec import Spec  # noqa: E402

EXAMPLES = ROOT / "examples"

SEARCH_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Search", "version": "1.0.0"},
    "paths": {
        "/search": {
            "get": {
                "operationId": "search",
                "parameters": [
                    {
                        "name": "q",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["q"],
                                    "properties": {"q": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


class LiveServer(unittest.TestCase):
    spec_factory = None
    options = None

    def setUp(self):
        spec = self.spec_factory()
        self.server = create_server(
            spec,
            host="127.0.0.1",
            port=0,
            options=self.options or ServerOptions(log_requests=False),
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response, body
        finally:
            connection.close()

    def json_body(self, response, body):
        return json.loads(body.decode("utf-8"))


class PetstoreServerTests(LiveServer):
    spec_factory = staticmethod(
        lambda: Spec(
            json.loads((EXAMPLES / "petstore.json").read_text(encoding="utf-8")),
            source=str(EXAMPLES / "petstore.json"),
        )
    )

    def test_list_pets(self):
        response, body = self.request("GET", "/v1/pets")
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type"))
        payload = self.json_body(response, body)
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)
        self.assertIn("name", payload[0])

    def test_route_and_seed_headers(self):
        response, _ = self.request("GET", "/v1/pets")
        self.assertEqual(response.getheader("X-Mog-Route"), "GET /v1/pets")
        self.assertIsNotNone(response.getheader("X-Mog-Seed"))

    def test_seeded_requests_match(self):
        _, first = self.request("GET", "/v1/pets?__seed=21")
        _, second = self.request("GET", "/v1/pets?__seed=21")
        self.assertEqual(first, second)

    def test_seed_header_override(self):
        response, _ = self.request("GET", "/v1/pets", {"X-Mog-Seed": "33"})
        self.assertEqual(response.getheader("X-Mog-Seed"), "33")

    def test_path_parameter_is_injected(self):
        response, body = self.request("GET", "/v1/pets/42")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.json_body(response, body)["id"], 42)

    def test_unknown_path(self):
        response, body = self.request("GET", "/v1/nothing")
        self.assertEqual(response.status, 404)
        self.assertEqual(self.json_body(response, body)["error"], "no route")

    def test_method_not_allowed(self):
        response, body = self.request("PUT", "/v1/pets")
        self.assertEqual(response.status, 405)
        self.assertIn("GET", response.getheader("Allow"))
        self.assertEqual(self.json_body(response, body)["allow"], ["get", "post"])

    def test_status_override(self):
        response, body = self.request("GET", "/v1/pets?__status=400")
        self.assertEqual(response.status, 400)
        self.assertIn("message", self.json_body(response, body))

    def test_status_override_204_has_no_body(self):
        response, body = self.request("GET", "/v1/pets?__status=204")
        self.assertEqual(response.status, 204)
        self.assertEqual(body, b"")

    def test_status_header_override(self):
        response, _ = self.request("GET", "/v1/pets", {"X-Mog-Status": "503"})
        self.assertEqual(response.status, 503)

    def test_invalid_status_override(self):
        response, body = self.request("GET", "/v1/pets?__status=abc")
        self.assertEqual(response.status, 400)
        self.assertIn("integer", self.json_body(response, body)["error"])

    def test_delete_returns_empty_204(self):
        response, body = self.request("DELETE", "/v1/pets/5")
        self.assertEqual(response.status, 204)
        self.assertEqual(body, b"")

    def test_health_endpoint(self):
        response, body = self.request("GET", "/__mog/health")
        payload = self.json_body(response, body)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["routes"], 6)

    def test_route_list_endpoint(self):
        response, body = self.request("GET", "/__mog/routes?method=POST")
        payload = self.json_body(response, body)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["routes"][0]["operationId"], "createPet")

    def test_spec_endpoint(self):
        response, body = self.request("GET", "/__mog/spec")
        payload = self.json_body(response, body)
        self.assertEqual(payload["kind"], "openapi")
        self.assertIn("openapi", payload["document"])

    def test_sample_endpoint(self):
        response, body = self.request("GET", "/__mog/sample?path=/v1/pets/9&__seed=4")
        payload = self.json_body(response, body)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["id"], 9)

    def test_sample_unknown_route(self):
        response, body = self.request("GET", "/__mog/sample?path=/nope")
        self.assertEqual(response.status, 404)

    def test_reset_endpoint(self):
        self.request("GET", "/v1/pets")
        response, body = self.request("POST", "/__mog/reset")
        self.assertEqual(self.json_body(response, body)["status"], "reset")
        response, body = self.request("GET", "/__mog/health")
        self.assertEqual(self.json_body(response, body)["requests"], 1)

    def test_reset_requires_post(self):
        response, _ = self.request("GET", "/__mog/reset")
        self.assertEqual(response.status, 405)

    def test_unknown_control_endpoint(self):
        response, _ = self.request("GET", "/__mog/nope")
        self.assertEqual(response.status, 404)

    def test_cors_headers(self):
        response, _ = self.request("GET", "/v1/pets")
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")

    def test_preflight(self):
        response, body = self.request(
            "OPTIONS",
            "/v1/pets",
            {"Access-Control-Request-Method": "GET", "Origin": "http://localhost:3000"},
        )
        self.assertEqual(response.status, 204)
        self.assertEqual(body, b"")
        self.assertIn("POST", response.getheader("Access-Control-Allow-Methods"))

    def test_delay_override(self):
        start = time.monotonic()
        response, _ = self.request("GET", "/v1/pets?__delay=80")
        elapsed = time.monotonic() - start
        self.assertEqual(response.status, 200)
        self.assertGreaterEqual(elapsed, 0.07)
        self.assertEqual(response.getheader("X-Mog-Latency-Ms"), "80")

    def test_invalid_delay(self):
        response, _ = self.request("GET", "/v1/pets?__delay=999999")
        self.assertEqual(response.status, 400)


class StrictServerTests(LiveServer):
    spec_factory = staticmethod(lambda: Spec(SEARCH_SPEC, source="search.json"))
    options = ServerOptions(strict=True, log_requests=False)

    def test_missing_required_parameter(self):
        response, body = self.request("GET", "/search")
        self.assertEqual(response.status, 400)
        self.assertEqual(self.json_body(response, body)["parameters"], ["q"])

    def test_present_required_parameter(self):
        response, body = self.request("GET", "/search?q=beacon")
        self.assertEqual(response.status, 200)
        self.assertIn("q", self.json_body(response, body))


class NoCorsServerTests(LiveServer):
    spec_factory = staticmethod(lambda: Spec(SEARCH_SPEC, source="search.json"))
    options = ServerOptions(cors=False, log_requests=False)

    def test_no_cors_headers(self):
        response, _ = self.request("GET", "/search?q=x")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(response.status, 200)


class RawServerTests(LiveServer):
    PAYLOAD = [{"event": "invoice.paid", "amount": 10}, {"event": "invoice.paid", "amount": 20}]

    spec_factory = staticmethod(
        lambda: Spec.from_payload(
            RawServerTests.PAYLOAD, source="payload.json", path="/events", method="POST", status=201
        )
    )

    def test_exact_payload(self):
        response, body = self.request("POST", "/events")
        self.assertEqual(response.status, 201)
        self.assertEqual(self.json_body(response, body), self.PAYLOAD)

    def test_wrong_method(self):
        response, _ = self.request("GET", "/events")
        self.assertEqual(response.status, 405)

    def test_sample_uses_inferred_schema(self):
        response, body = self.request("GET", "/__mog/sample?path=/events&method=POST&__seed=2")
        payload = self.json_body(response, body)
        self.assertIsInstance(payload, list)
        self.assertIn("event", payload[0])

    def test_spec_endpoint_for_payload(self):
        response, body = self.request("GET", "/__mog/spec")
        payload = self.json_body(response, body)
        self.assertEqual(payload["kind"], "json")
        self.assertEqual(payload["payload"], self.PAYLOAD)
        self.assertEqual(payload["inferred_schema"]["type"], "array")


if __name__ == "__main__":
    unittest.main()
