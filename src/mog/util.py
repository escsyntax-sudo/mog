"""Shared document loading and path helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import unquote, urlsplit


class SpecError(Exception):
    """Raised when a document cannot be loaded, parsed or understood."""


def read_source(source: str) -> str:
    if re.match(r"^https?://", source, re.IGNORECASE):
        from urllib.request import Request, urlopen

        request = Request(
            source,
            headers={
                "Accept": "application/json, application/yaml, text/yaml, */*",
                "User-Agent": "mog",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except Exception as exc:
            raise SpecError(f"could not fetch {source}: {exc}") from exc

    path = Path(source).expanduser()
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise SpecError(f"file not found: {source}") from exc
    except OSError as exc:
        raise SpecError(f"could not read {source}: {exc}") from exc


def parse_document(text: str, source: str = "<memory>") -> Any:
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        raise SpecError(f"empty document: {source}")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as json_error:
        json_message = (
            f"invalid JSON in {source}: line {json_error.lineno} "
            f"column {json_error.colno}: {json_error.msg}"
        )

    try:
        import yaml
    except ImportError as exc:
        raise SpecError(
            f"{json_message}. YAML input needs PyYAML, install it with: pip install 'mog[yaml]'"
        ) from exc

    try:
        return yaml.safe_load(stripped)
    except yaml.YAMLError as exc:
        raise SpecError(f"could not parse {source} as JSON or YAML: {json_message}") from exc


def resolve_ref(document: Any, ref: str) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SpecError(f"external $ref is not supported: {ref}")
    node = document
    for raw in ref[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError) as exc:
                raise SpecError(f"unresolved $ref: {ref}") from exc
        elif isinstance(node, dict):
            if part not in node:
                raise SpecError(f"unresolved $ref: {ref}")
            node = node[part]
        else:
            raise SpecError(f"unresolved $ref: {ref}")
    return node


def join_paths(*parts: str) -> str:
    segments = [part.strip("/") for part in parts if part and part.strip("/")]
    return "/" + "/".join(segments) if segments else "/"


def compile_template(template: str) -> Tuple[re.Pattern[str], List[str]]:
    if not template.startswith("/"):
        template = "/" + template
    body = template.strip("/")
    segments = body.split("/") if body else []
    parts: List[str] = []
    names: List[str] = []
    for segment in segments:
        if segment.startswith("{") and segment.endswith("}") and len(segment) > 2:
            inner = segment[1:-1].strip()
            if inner.endswith("*"):
                inner = inner[:-1]
            name = inner.split(":", 1)[0].strip() or "param"
            names.append(name)
            parts.append(r"([^/]+)")
        else:
            parts.append(re.escape(segment))
    pattern = "^/" + "/".join(parts) + "/?$" if parts else "^/?$"
    return re.compile(pattern), names


def status_to_int(code: Any) -> int:
    text = str(code).strip()
    if text.lower() == "default":
        return 200
    upper = text.upper()
    if upper.endswith("XX") and len(upper) == 3 and upper[0].isdigit():
        return int(upper[0]) * 100
    try:
        return int(text)
    except ValueError:
        return 200


def decode_path(path: str) -> str:
    return unquote(path) or "/"


def split_url(path: str):
    return urlsplit(path)
