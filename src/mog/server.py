"""Threaded mock HTTP server."""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from xml.sax.saxutils import escape as xml_escape

from . import __version__
from .generator import Faker
from .spec import ResponseSpec, Route, Spec, resolve_response

logger = logging.getLogger("mog.request")


@dataclass
class ServerOptions:
    seed: int = 0
    latency_ms: int = 0
    jitter_ms: int = 0
    cors: bool = True
    strict: bool = False
    include_optional: bool = True
    use_examples: bool = True
    max_depth: int = 6
    null_probability: float = 0.0
    log_requests: bool = True


@dataclass
class BuiltResponse:
    status: int
    media_type: str
    body: Any
    headers: Dict[str, str] = field(default_factory=dict)
    has_body: bool = True


@dataclass
class Prepared:
    status: int
    media_type: str
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)


class MockState:
    def __init__(self, spec: Spec, options: ServerOptions) -> None:
        self.spec = spec
        self.options = options
        self.started = time.monotonic()
        self.requests = 0
        self.seed_counter = 0
        self._lock = threading.Lock()

    def next_seed(self, override: Optional[int] = None) -> int:
        if override is not None:
            return int(override)
        with self._lock:
            self.seed_counter += 1
            count = self.seed_counter
        return int(self.options.seed + count)

    def note_request(self) -> None:
        with self._lock:
            self.requests += 1

    def reset(self) -> None:
        with self._lock:
            self.started = time.monotonic()
            self.requests = 0
            self.seed_counter = 0

    def health(self) -> Dict[str, Any]:
        spec = self.spec
        return {
            "status": "ok",
            "name": "mog",
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - self.started, 2),
            "requests": self.requests,
            "seed": self.options.seed,
            "routes": len(spec.routes),
            "spec": {
                "kind": spec.kind,
                "format": spec.format,
                "title": spec.title,
                "version": spec.api_version,
                "source": spec.source,
            },
        }

    def spec_payload(self) -> Dict[str, Any]:
        spec = self.spec
        if spec.kind == "json":
            from .generator import infer_schema

            return {
                "kind": "json",
                "source": spec.source,
                "payload": spec.document,
                "inferred_schema": infer_schema(spec.document),
            }
        return {"kind": "openapi", "source": spec.source, "document": spec.document}


def _xml_tag(name: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))
    if not cleaned:
        return "item"
    if cleaned[0].isdigit():
        return "_" + cleaned
    return cleaned


def _to_xml(value: Any, tag: str = "response") -> str:
    tag = _xml_tag(tag)
    if isinstance(value, dict):
        inner = "".join(_to_xml(sub, str(key)) for key, sub in value.items())
        return f"<{tag}>{inner}</{tag}>"
    if isinstance(value, list):
        return f"<{tag}>" + "".join(_to_xml(sub, "item") for sub in value) + f"</{tag}>"
    if value is None:
        return f"<{tag}/>"
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return f"<{tag}>{xml_escape(text)}</{tag}>"


def encode_body(media_type: str, body: Any, has_body: bool = True) -> bytes:
    if not has_body or body is None:
        return b""
    media = (media_type or "application/json").lower()
    if "json" in media or media in ("*/*", ""):
        return json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    if "xml" in media:
        return _to_xml(body).encode("utf-8")
    if "x-www-form-urlencoded" in media and isinstance(body, dict):
        return urlencode(body, doseq=True).encode("utf-8")
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def content_type_for(media_type: str) -> str:
    if not media_type:
        return ""
    lower = media_type.lower()
    if "charset=" in lower:
        return media_type
    if (
        lower.startswith("text/")
        or "json" in lower
        or "xml" in lower
        or lower == "application/javascript"
    ):
        return media_type + "; charset=utf-8"
    return media_type


def _coerce(value: str, template: Any) -> Any:
    if isinstance(template, bool):
        return str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(template, float):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _param_candidates(name: str) -> List[str]:
    raw = str(name or "").strip()
    if not raw:
        return []
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", raw).lower()
    snake = re.sub(r"[^a-z0-9_]+", "_", snake).strip("_")
    candidates = [raw, raw.lower()]
    if snake and snake not in candidates:
        candidates.append(snake)
    if snake.endswith("_id") and len(snake) > 3:
        candidates.append("id")
        prefix = snake[:-3]
        if prefix and prefix not in candidates:
            candidates.append(prefix)
    return candidates


def _inject_params(body: Any, params: Dict[str, str]) -> Any:
    if not params:
        return body

    def apply(item: Dict[str, Any]) -> None:
        for name, value in params.items():
            for candidate in _param_candidates(name):
                if candidate in item:
                    item[candidate] = _coerce(value, item[candidate])
                    break

    if isinstance(body, dict):
        apply(body)
    elif isinstance(body, list):
        for entry in body[:50]:
            if isinstance(entry, dict):
                apply(entry)
    return body


def build_response(
    spec: Spec,
    route: Route,
    response: ResponseSpec,
    seed: int,
    options: ServerOptions,
    params: Optional[Dict[str, str]] = None,
) -> BuiltResponse:
    params = params or {}
    document = spec.document if spec.kind == "openapi" else None
    faker = Faker(
        document=document,
        seed=seed,
        include_optional=options.include_optional,
        use_examples=options.use_examples,
        max_depth=options.max_depth,
        null_probability=options.null_probability,
    )

    if route.raw:
        body = route.payload if response.has_body else None
    elif response.has_example and options.use_examples:
        body = copy.deepcopy(response.example)
    elif response.schema is not None:
        body = faker.generate(response.schema, route.operation_id)
    elif response.has_body:
        body = {}
    else:
        body = None

    if body is not None and not route.raw:
        body = _inject_params(body, params)

    headers: Dict[str, str] = {}
    for name, header in (response.headers or {}).items():
        if not isinstance(header, dict):
            continue
        lower = str(name).lower()
        if lower in (
            "content-type",
            "content-length",
            "transfer-encoding",
            "connection",
        ):
            continue
        if "example" in header:
            value = copy.deepcopy(header["example"])
        elif isinstance(header.get("schema"), dict):
            value = faker.generate(header["schema"], str(name))
        else:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        headers[str(name)] = str(value)

    return BuiltResponse(
        status=response.status,
        media_type=response.media_type,
        body=body,
        headers=headers,
        has_body=response.has_body and response.status not in (204, 304),
    )


class MockHandler(BaseHTTPRequestHandler):
    server_version = f"mog/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_TRACE(self) -> None:
        self._dispatch()

    def _read_body(self) -> None:
        length = self.headers.get("Content-Length")
        if not length:
            return
        try:
            size = int(length)
        except ValueError:
            return
        if size > 0:
            self.rfile.read(min(size, 2_000_000))

    def _dispatch(self) -> None:
        start = time.monotonic()
        self._read_body()
        try:
            prepared = self._prepare()
        except Exception as exc:
            logger.exception("mock handler failed")
            body = json.dumps(
                {"error": "mock server failure", "detail": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
            prepared = Prepared(
                500, "application/json; charset=utf-8", body, {}
            )
        self._send(prepared, start)

    def _send(self, prepared: Prepared, start: float) -> None:
        try:
            self.send_response(prepared.status)
            for name, value in prepared.headers.items():
                self.send_header(name, value)
            if prepared.media_type:
                self.send_header("Content-Type", prepared.media_type)
            self.send_header("Content-Length", str(len(prepared.body)))
            self.end_headers()
            if self.command != "HEAD" and prepared.body:
                self.wfile.write(prepared.body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            state = self.server.state
            state.note_request()
            if state.options.log_requests:
                elapsed = (time.monotonic() - start) * 1000
                logger.info(
                    "%s %s %d %.1fms",
                    self.command,
                    self.path,
                    prepared.status,
                    elapsed,
                )

    def _cors_headers(self, full: bool = False) -> Dict[str, str]:
        state = self.server.state
        if not state.options.cors:
            return {}
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "X-Mog-Route, X-Mog-Seed, X-Mog-Latency-Ms",
        }
        if full:
            headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
            )
            requested = self.headers.get("Access-Control-Request-Headers")
            headers["Access-Control-Allow-Headers"] = requested or (
                "Content-Type, Authorization, X-Mog-Status, X-Mog-Delay, X-Mog-Seed"
            )
            headers["Access-Control-Max-Age"] = "600"
        return headers

    def _json(
        self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None
    ) -> Prepared:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        merged = dict(headers or {})
        merged.update(self._cors_headers())
        return Prepared(
            status, "application/json; charset=utf-8", body, merged
        )

    def _error(self, status: int, payload: Dict[str, Any], headers=None) -> Prepared:
        return self._json(status, payload, headers)

    def _prepare(self) -> Prepared:
        state = self.server.state
        options = state.options
        parsed = urlsplit(self.path)
        path = unquote(parsed.path) or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)

        if self.command == "OPTIONS" and options.cors:
            headers = self._cors_headers(full=True)
            headers["Allow"] = "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
            return Prepared(204, "", b"", headers)

        if path == "/__mog" or path.startswith("/__mog/"):
            return self._control(path, query)

        route, params, allowed = state.spec.match(self.command, path)
        if route is None:
            if allowed:
                headers = {"Allow": ", ".join(method.upper() for method in allowed)}
                return self._error(
                    405,
                    {
                        "error": "method not allowed",
                        "method": self.command,
                        "path": path,
                        "allow": allowed,
                    },
                    headers,
                )
            return self._error(
                404,
                {
                    "error": "no route",
                    "method": self.command,
                    "path": path,
                    "hint": "GET /__mog/routes lists known paths",
                },
            )

        if options.strict:
            missing: List[str] = []
            for param in route.parameters:
                if not param.get("required"):
                    continue
                location = param.get("in")
                name = str(param.get("name") or "")
                if not name:
                    continue
                if location == "query" and name not in query:
                    missing.append(name)
                elif location == "header" and not self.headers.get(name):
                    missing.append(name)
            if missing:
                return self._error(
                    400,
                    {
                        "error": "missing required parameter",
                        "parameters": missing,
                    },
                )

        status_override, status_error = self._override_status(query)
        if status_error:
            return self._error(400, {"error": status_error})
        delay_override, delay_error = self._override_delay(query)
        if delay_error:
            return self._error(400, {"error": delay_error})
        seed_override, seed_error = self._override_seed(query)
        if seed_error:
            return self._error(400, {"error": seed_error})

        seed = state.next_seed(seed_override)
        response = resolve_response(
            state.spec.version, route, status_override
        )
        built = build_response(state.spec, route, response, seed, options, params)

        delay = options.latency_ms + delay_override
        if options.jitter_ms > 0:
            delay += random.Random(seed).randint(0, options.jitter_ms)
        if delay > 0:
            time.sleep(delay / 1000.0)

        if built.status in (204, 304):
            body = b""
            media_type = ""
        else:
            body = encode_body(built.media_type, built.body, built.has_body)
            media_type = content_type_for(built.media_type) if body else ""

        headers = dict(built.headers)
        headers.update(self._cors_headers())
        headers["X-Mog-Route"] = f"{route.method.upper()} {route.template}"
        headers["X-Mog-Seed"] = str(seed)
        if delay:
            headers["X-Mog-Latency-Ms"] = str(int(delay))

        return Prepared(built.status, media_type, body, headers)

    def _query_int(
        self, query: Dict[str, List[str]], key: str, low: int, high: int, label: str
    ):
        values = query.get(key)
        if not values:
            return None, None
        try:
            value = int(values[-1])
        except ValueError:
            return None, f"{label} must be an integer"
        if value < low or value > high:
            return None, f"{label} must be between {low} and {high}"
        return value, None

    def _override_status(self, query: Dict[str, List[str]]):
        text: Optional[str] = None
        if query.get("__status"):
            text = query["__status"][-1]
        elif self.headers.get("X-Mog-Status"):
            text = self.headers.get("X-Mog-Status")
        if text is None:
            return None, None
        try:
            value = int(text)
        except ValueError:
            return None, "status override must be an integer"
        if value < 100 or value > 599:
            return None, "status override must be between 100 and 599"
        return value, None

    def _override_delay(self, query: Dict[str, List[str]]):
        text: Optional[str] = None
        if query.get("__delay"):
            text = query["__delay"][-1]
        elif self.headers.get("X-Mog-Delay"):
            text = self.headers.get("X-Mog-Delay")
        if text is None:
            return 0, None
        try:
            value = int(text)
        except ValueError:
            return 0, "delay override must be an integer"
        if value < 0 or value > 60_000:
            return 0, "delay override must be between 0 and 60000"
        return value, None

    def _override_seed(self, query: Dict[str, List[str]]):
        text: Optional[str] = None
        if query.get("__seed"):
            text = query["__seed"][-1]
        elif self.headers.get("X-Mog-Seed"):
            text = self.headers.get("X-Mog-Seed")
        if text is None:
            return None, None
        try:
            return int(text), None
        except ValueError:
            return None, "seed override must be an integer"

    def _control(self, path: str, query: Dict[str, List[str]]) -> Prepared:
        state = self.server.state
        if path in ("/__mog", "/__mog/"):
            return self._json(
                200,
                {
                    "name": "mog",
                    "version": __version__,
                    "endpoints": [
                        "GET /__mog/health",
                        "GET /__mog/routes",
                        "GET /__mog/spec",
                        "GET /__mog/sample?path=/pets&method=GET",
                        "POST /__mog/reset",
                    ],
                },
            )
        if path == "/__mog/health":
            return self._json(200, state.health())
        if path == "/__mog/routes":
            routes = state.spec.routes
            method = query.get("method")
            if method:
                wanted = method[-1].lower()
                routes = [route for route in routes if route.method == wanted]
            tag = query.get("tag")
            if tag:
                routes = [route for route in routes if tag[-1] in route.tags]
            return self._json(
                200,
                {
                    "count": len(routes),
                    "routes": [route.to_dict() for route in routes],
                },
            )
        if path == "/__mog/spec":
            return self._json(200, state.spec_payload())
        if path == "/__mog/sample":
            return self._sample(query)
        if path == "/__mog/reset":
            if self.command != "POST":
                return self._error(
                    405,
                    {"error": "method not allowed", "allow": ["POST"]},
                    {"Allow": "POST"},
                )
            state.reset()
            return self._json(200, {"status": "reset"})
        return self._error(
            404, {"error": "unknown control endpoint", "path": path}
        )

    def _sample(self, query: Dict[str, List[str]]) -> Prepared:
        state = self.server.state
        method = (query.get("method") or ["GET"])[-1].upper()
        path = (query.get("path") or ["/"])[-1]
        route, params, _ = state.spec.match(method, path)
        if route is None:
            route = state.spec.find(method, path)
            if route is not None:
                params = {name: "1" for name in route.param_names}
        if route is None:
            return self._error(
                404,
                {
                    "error": "no route",
                    "method": method,
                    "path": path,
                    "hint": "GET /__mog/routes lists known paths",
                },
            )
        status, error = self._query_int(
            query, "__status", 100, 599, "status"
        )
        if error:
            return self._error(400, {"error": error})
        seed, error = self._query_int(
            query, "__seed", -(2**63), 2**63 - 1, "seed"
        )
        if error:
            return self._error(400, {"error": error})
        if seed is None:
            seed = state.options.seed
        response = resolve_response(state.spec.version, route, status)
        built = build_response(
            state.spec, route, response, seed, state.options, params
        )
        return self._json(built.status, built.body, built.headers)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: MockState) -> None:
        self.state = state
        super().__init__(address, MockHandler)


def create_server(
    spec: Spec,
    host: str = "127.0.0.1",
    port: int = 8000,
    options: Optional[ServerOptions] = None,
) -> MockServer:
    state = MockState(spec, options or ServerOptions())
    return MockServer((host, port), state)
