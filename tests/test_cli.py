import contextlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mog.cli import main  # noqa: E402

EXAMPLES = ROOT / "examples"
PETSTORE = str(EXAMPLES / "petstore.json")
SWAGGER = str(EXAMPLES / "swagger.json")
PAYLOAD = str(EXAMPLES / "payload.json")


def run(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class RoutesCommandTests(unittest.TestCase):
    def test_table_output(self):
        code, out, _ = run(["routes", PETSTORE])
        self.assertEqual(code, 0)
        self.assertIn("METHOD", out)
        self.assertIn("GET", out)
        self.assertIn("/v1/pets", out)
        self.assertIn("List pets", out)

    def test_json_output(self):
        code, out, _ = run(["routes", PETSTORE, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "openapi")
        self.assertEqual(payload["count"], 6)
        methods = {route["method"] for route in payload["routes"]}
        self.assertEqual(methods, {"GET", "POST", "DELETE"})

    def test_prefix_flag(self):
        code, out, _ = run(["routes", PETSTORE, "--prefix", "/mock", "--json"])
        payload = json.loads(out)
        self.assertTrue(all(route["path"].startswith("/mock/") for route in payload["routes"]))

    def test_swagger_output(self):
        code, out, _ = run(["routes", SWAGGER, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "openapi")
        self.assertEqual(payload["version"], "1.0.0")
        paths = {route["path"] for route in payload["routes"]}
        self.assertIn("/api/tasks/{id}", paths)

    def test_raw_payload_output(self):
        code, out, _ = run(["routes", PAYLOAD, "--path", "/events", "--method", "POST"])
        self.assertEqual(code, 0)
        self.assertIn("/events", out)
        self.assertIn("POST", out)


class SampleCommandTests(unittest.TestCase):
    def sample(self, *extra):
        code, out, err = run(["sample", PETSTORE, "--seed", "3", *extra])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_first_route(self):
        payload = self.sample()
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["request"], "GET /v1/pets")
        self.assertIsInstance(payload["body"], list)
        self.assertTrue(payload["body"])

    def test_route_index(self):
        payload = self.sample("--index", "2")
        self.assertEqual(payload["request"], "GET /v1/pets/{petId}")

    def test_path_parameter_is_reflected(self):
        payload = self.sample("--path", "/v1/pets/77")
        self.assertEqual(payload["body"]["id"], 77)
        self.assertEqual(payload["status"], 200)

    def test_status_flag(self):
        payload = self.sample("--path", "/v1/pets", "--status", "400")
        self.assertEqual(payload["status"], 400)
        self.assertIn("message", payload["body"])

    def test_optional_is_off(self):
        payload = self.sample("--path", "/v1/pets/1", "--no-optional")
        self.assertEqual(
            set(payload["body"].keys()),
            {"id", "name", "species", "status"},
        )

    def test_examples_are_off(self):
        code, out, err = run(
            ["sample", PETSTORE, "--path", "/v1/health", "--no-examples", "--seed", "1"]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["body"], {})

    def test_raw_payload_is_exact(self):
        code, out, err = run(
            [
                "sample",
                PAYLOAD,
                "--path",
                "/events",
                "--method",
                "POST",
                "--status",
                "201",
            ]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["request"], "POST /events")
        self.assertEqual(payload["status"], 201)
        self.assertEqual(payload["body"], json.loads(Path(PAYLOAD).read_text(encoding="utf-8")))

    def test_seed_is_reproducible(self):
        first = self.sample("--path", "/v1/pets/3")
        second = self.sample("--path", "/v1/pets/3")
        self.assertEqual(first, second)


class ErrorTests(unittest.TestCase):
    def test_missing_file(self):
        code, out, err = run(["routes", "does-not-exist.json"])
        self.assertEqual(code, 1)
        self.assertIn("file not found", err)
        self.assertEqual(out, "")

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{ broken", encoding="utf-8")
            code, _, err = run(["routes", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", err)

    def test_unknown_sample_route(self):
        code, _, err = run(["sample", PETSTORE, "--path", "/v1/missing"])
        self.assertEqual(code, 1)
        self.assertIn("no route", err)

    def test_route_index_out_of_range(self):
        code, _, err = run(["sample", PETSTORE, "--index", "99"])
        self.assertEqual(code, 1)
        self.assertIn("out of range", err)

    def test_bad_port(self):
        code, _, err = run(["serve", PETSTORE, "--port", "70000"])
        self.assertEqual(code, 1)
        self.assertIn("port", err)

    def test_no_command_prints_help(self):
        code, out, _ = run([])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_version(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as caught:
                main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("mog", out.getvalue())


class ServeCommandTests(unittest.TestCase):
    def test_serve_answers_requests(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mog",
                "serve",
                PETSTORE,
                "--port",
                "0",
                "--quiet",
                "--seed",
                "5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        lines = []
        port = None
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if not line:
                    break
                lines.append(line)
                match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    break
            if port is None:
                process.terminate()
                rest_out, rest_err = process.communicate(timeout=10)
                self.fail(
                    "server did not start: "
                    + "".join(lines)
                    + rest_out
                    + rest_err
                )

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/v1/pets")
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(body.decode("utf-8")))
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
