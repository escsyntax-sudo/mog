"""OpenAPI, Swagger and raw JSON loading with route compilation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import (
    SpecError,
    compile_template,
    join_paths,
    resolve_ref,
    status_to_int,
)

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


@dataclass
class Route:
    method: str
    template: str
    regex: re.Pattern[str]
    param_names: List[str]
    operation: Dict[str, Any]
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    responses: Dict[str, Any] = field(default_factory=dict)
    produces: List[str] = field(default_factory=list)
    source_index: int = 0
    raw: bool = False
    payload: Any = None

    @property
    def operation_id(self) -> str:
        value = self.operation.get("operationId")
        return str(value) if value else ""

    @property
    def summary(self) -> str:
        return str(self.operation.get("summary") or "")

    @property
    def tags(self) -> List[str]:
        tags = self.operation.get("tags")
        if isinstance(tags, list):
            return [str(tag) for tag in tags]
        return []

    def specificity(self) -> int:
        static_segments = 0
        static_chars = 0
        for segment in self.template.strip("/").split("/"):
            if not segment or (segment.startswith("{") and segment.endswith("}")):
                continue
            static_segments += 1
            static_chars += len(segment)
        return static_segments * 1000 + static_chars

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method.upper(),
            "path": self.template,
            "operationId": self.operation_id,
            "summary": self.summary,
            "tags": self.tags,
            "statuses": [str(code) for code in self.responses.keys()],
            "produces": list(self.produces),
            "raw": self.raw,
            "parameters": [
                {
                    "name": param.get("name"),
                    "in": param.get("in"),
                    "required": bool(param.get("required")),
                }
                for param in self.parameters
                if isinstance(param, dict)
            ],
        }


@dataclass
class ResponseSpec:
    code: str
    status: int
    media_type: str
    schema: Optional[Dict[str, Any]] = None
    example: Any = None
    has_example: bool = False
    headers: Dict[str, Any] = field(default_factory=dict)
    has_body: bool = True


def _pick_media(media_types: List[str]) -> str:
    if not media_types:
        return "application/json"
    for name in media_types:
        if name.lower().split(";")[0] == "application/json":
            return name
    for name in media_types:
        if "json" in name.lower():
            return name
    return media_types[0]


def _default_code(keys: List[str]) -> str:
    for key in keys:
        if key.lower() != "default" and status_to_int(key) == 200:
            return key
    for key in sorted(keys, key=status_to_int):
        if key.lower() != "default" and 200 <= status_to_int(key) < 300:
            return key
    for key in keys:
        if key.lower() == "default":
            return key
    return keys[0] if keys else "200"


def _pick_response_code(responses: Dict[str, Any], requested: Optional[int]):
    keys = [str(key) for key in responses.keys()]
    if requested is None:
        return _default_code(keys), False
    text = str(requested)
    for key in keys:
        if key == text:
            return key, True
    for key in keys:
        upper = key.upper()
        if upper.endswith("XX") and upper[0] == text[0]:
            return key, True
    for key in keys:
        if key.lower() != "default" and status_to_int(key) == requested:
            return key, True
    return text, False


def resolve_response(
    version: str, route: Route, requested_status: Optional[int] = None
) -> ResponseSpec:
    code, exact = _pick_response_code(route.responses, requested_status)
    status = requested_status if requested_status is not None else status_to_int(code)

    if requested_status is not None and not exact:
        if status in (204, 304):
            return ResponseSpec(
                code=str(status), status=status, media_type="", has_body=False
            )
        example = None
        has_example = False
        if status >= 400:
            example = {"error": "simulated error", "status": status}
            has_example = True
        return ResponseSpec(
            code=str(status),
            status=status,
            media_type="application/json",
            example=example,
            has_example=has_example,
        )

    response = route.responses.get(code)
    if not isinstance(response, dict):
        response = {}

    schema: Optional[Dict[str, Any]] = None
    example: Any = None
    has_example = False
    media_type = "application/json"

    if version == "3":
        content = response.get("content")
        if isinstance(content, dict) and content:
            media_type = _pick_media([str(key) for key in content.keys()])
            media_object = content.get(media_type)
            if not isinstance(media_object, dict):
                media_object = next(
                    (value for value in content.values() if isinstance(value, dict)), {}
                )
            media_schema = media_object.get("schema")
            if isinstance(media_schema, dict):
                schema = media_schema
            if "example" in media_object:
                example = media_object["example"]
                has_example = True
            examples = media_object.get("examples")
            if not has_example and isinstance(examples, dict) and examples:
                first = next(iter(examples.values()))
                if isinstance(first, dict) and "value" in first:
                    example = first["value"]
                else:
                    example = first
                has_example = True
            has_body = True
        else:
            has_body = False
    else:
        schema_value = response.get("schema")
        if isinstance(schema_value, dict):
            schema = schema_value
        media_type = _pick_media(route.produces)
        examples = response.get("examples")
        if isinstance(examples, dict) and examples:
            if media_type in examples:
                example = examples[media_type]
            else:
                example = next(iter(examples.values()))
            has_example = True
        has_body = schema is not None or has_example
        if status in (204, 304):
            has_body = False

    headers = response.get("headers")
    if not isinstance(headers, dict):
        headers = {}

    return ResponseSpec(
        code=code,
        status=status,
        media_type=media_type,
        schema=schema,
        example=example,
        has_example=has_example,
        headers=headers,
        has_body=has_body,
    )


def _server_base_path(document: Dict[str, Any], version: str) -> str:
    if version == "3":
        servers = document.get("servers")
        if not isinstance(servers, list) or not servers:
            return ""
        server = servers[0]
        if not isinstance(server, dict):
            return ""
        url = str(server.get("url") or "")
        variables = server.get("variables")
        if isinstance(variables, dict):
            for name, variable in variables.items():
                if isinstance(variable, dict) and "default" in variable:
                    url = url.replace("{" + str(name) + "}", str(variable["default"]))
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            from urllib.parse import urlsplit

            url = urlsplit(url).path
        if url and not url.startswith("/"):
            url = "/" + url
        return "" if url in ("", "/") else url.rstrip("/")

    base = document.get("basePath")
    if not isinstance(base, str) or not base:
        return ""
    if not base.startswith("/"):
        base = "/" + base
    return "" if base == "/" else base.rstrip("/")


def _merge_parameters(
    document: Dict[str, Any],
    path_parameters: Any,
    operation_parameters: Any,
) -> List[Dict[str, Any]]:
    merged: Dict[Any, Dict[str, Any]] = {}
    for source in (path_parameters, operation_parameters):
        if not isinstance(source, list):
            continue
        for raw in source:
            param = raw
            if isinstance(param, dict) and "$ref" in param:
                param = resolve_ref(document, str(param["$ref"]))
            if not isinstance(param, dict):
                continue
            key = (param.get("in"), param.get("name"))
            merged[key] = param
    return list(merged.values())


def _build_routes(document: Dict[str, Any], prefix: str) -> List[Route]:
    version = "3" if "openapi" in document else "2"
    base = _server_base_path(document, version)
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SpecError("document has no paths object")
    produces = document.get("produces") if version == "2" else None
    default_produces = (
        [str(item) for item in produces] if isinstance(produces, list) else []
    )

    routes: List[Route] = []
    for template, raw_item in paths.items():
        if not isinstance(template, str) or not template.startswith("/"):
            continue
        item = raw_item
        if isinstance(item, dict) and "$ref" in item:
            item = resolve_ref(document, str(item["$ref"]))
        if not isinstance(item, dict):
            continue
        path_parameters = item.get("parameters")
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                operation = item.get(method.upper())
            if not isinstance(operation, dict):
                continue

            responses: Dict[str, Any] = {}
            raw_responses = operation.get("responses")
            if isinstance(raw_responses, dict):
                for code, response in raw_responses.items():
                    if isinstance(response, dict) and "$ref" in response:
                        response = resolve_ref(document, str(response["$ref"]))
                    responses[str(code)] = response if isinstance(response, dict) else {}
            if not responses:
                responses = {"200": {"description": "OK"}}

            route_produces = default_produces
            if version == "2" and isinstance(operation.get("produces"), list):
                route_produces = [str(item) for item in operation["produces"]]

            full_template = join_paths(prefix, base, template)
            regex, names = compile_template(full_template)
            routes.append(
                Route(
                    method=method,
                    template=full_template,
                    regex=regex,
                    param_names=names,
                    operation=operation,
                    parameters=_merge_parameters(
                        document, path_parameters, operation.get("parameters")
                    ),
                    responses=responses,
                    produces=route_produces,
                    source_index=len(routes),
                )
            )
    return routes


class Spec:
    def __init__(
        self, document: Any, source: str = "<memory>", prefix: str = ""
    ) -> None:
        if not isinstance(document, dict):
            raise SpecError("OpenAPI document must be a JSON or YAML object")
        if "openapi" not in document and "swagger" not in document:
            raise SpecError("document has no openapi or swagger version field")
        self.document = document
        self.source = source
        self.prefix = prefix
        self.kind = "openapi"
        self.version = "3" if "openapi" in document else "2"
        self.format = str(document.get("openapi") or document.get("swagger") or "")
        info = document.get("info")
        info = info if isinstance(info, dict) else {}
        self.title = str(info.get("title") or "")
        self.api_version = str(info.get("version") or "")
        self.routes = _build_routes(document, prefix)

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        source: str = "<memory>",
        path: str = "/",
        method: str = "GET",
        status: int = 200,
        prefix: str = "",
    ) -> "Spec":
        from .generator import infer_schema

        self = cls.__new__(cls)
        self.document = payload
        self.source = source
        self.prefix = prefix
        self.kind = "json"
        self.version = "3"
        self.format = "raw JSON"
        stem = Path(source).stem if source and source != "<memory>" else "payload"
        self.title = stem
        self.api_version = ""

        template = join_paths(prefix, path)
        regex, names = compile_template(template)
        schema = infer_schema(payload)
        operation = {
            "summary": "Raw JSON payload",
            "operationId": "rawPayload",
            "responses": {
                str(status): {
                    "description": "Raw payload",
                    "content": {"application/json": {"schema": schema}},
                }
            },
        }
        self.routes = [
            Route(
                method=method.lower(),
                template=template,
                regex=regex,
                param_names=names,
                operation=operation,
                responses={
                    str(status): {
                        "description": "Raw payload",
                        "content": {"application/json": {"schema": schema}},
                    }
                },
                produces=["application/json"],
                source_index=0,
                raw=True,
                payload=payload,
            )
        ]
        return self

    def match(self, method: str, path: str):
        method = method.lower()
        if not path:
            path = "/"
        matches = []
        for route in self.routes:
            found = route.regex.match(path)
            if found:
                matches.append((route, found))
        if not matches:
            return None, {}, []
        allowed = sorted({route.method for route, _ in matches})
        candidates = [
            (route, found) for route, found in matches if route.method == method
        ]
        if not candidates:
            return None, {}, allowed
        candidates.sort(key=lambda item: (-item[0].specificity(), item[0].source_index))
        route, found = candidates[0]
        params = {
            name: value
            for name, value in zip(route.param_names, found.groups())
        }
        return route, params, allowed

    def find(self, method: str, path: str) -> Optional[Route]:
        route, _, _ = self.match(method, path)
        if route is not None:
            return route
        template = path if path.startswith("/") else "/" + path
        if template != "/":
            template = template.rstrip("/") or "/"
        for candidate in self.routes:
            if candidate.method == method.lower() and candidate.template == template:
                return candidate
        return None

    def routes_for_tag(self, tag: str) -> List[Route]:
        return [route for route in self.routes if tag in route.tags]

    def describe(self) -> str:
        name = Path(self.source).name if self.source else "<memory>"
        if self.kind == "json":
            return f"{name} (raw JSON, {len(self.routes)} route)"
        return f"{name} ({'OpenAPI' if self.version == '3' else 'Swagger'} {self.format}, {len(self.routes)} routes)"
