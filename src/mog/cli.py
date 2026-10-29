"""Command line interface for MOG."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .generator import Faker
from .server import ServerOptions, build_response, create_server
from .spec import ResponseSpec, Route, Spec, resolve_response
from .util import SpecError, parse_document, read_source


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mog",
        description=(
            "Serve OpenAPI, Swagger or raw JSON documents as a mock HTTP API "
            "with seeded fake data."
        ),
        epilog=(
            "Examples:\n"
            "  mog serve examples/petstore.json\n"
            "  mog routes examples/petstore.json --method get\n"
            "  mog sample examples/petstore.json --path /pets/7\n"
            "  mog serve payload.json --path /events --method POST\n"
            "  mog serve api.yaml --port 3000 --latency 250"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"mog {__version__}")

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--prefix",
        default="",
        metavar="PATH",
        help="mount every route under this path prefix",
    )
    shared.add_argument(
        "--seed",
        type=int,
        default=0,
        metavar="N",
        help="seed for generated data (default: 0)",
    )
    shared.add_argument(
        "--no-optional",
        action="store_true",
        help="omit properties that are not listed as required",
    )
    shared.add_argument(
        "--no-examples",
        action="store_true",
        help="ignore example and default values in the document",
    )
    shared.add_argument(
        "--max-depth",
        type=int,
        default=6,
        metavar="N",
        help="maximum nesting depth for generated values (default: 6)",
    )
    shared.add_argument(
        "--nulls",
        type=float,
        default=0.0,
        metavar="RATE",
        help="chance from 0 to 1 that a nullable field is null (default: 0)",
    )

    raw = argparse.ArgumentParser(add_help=False)
    raw.add_argument(
        "--path",
        default=None,
        metavar="PATH",
        help="serve a raw JSON payload at this path instead of reading a spec",
    )
    raw.add_argument(
        "--method",
        default="GET",
        metavar="METHOD",
        help="HTTP method for a raw JSON payload (default: GET)",
    )
    raw.add_argument(
        "--status",
        type=int,
        default=None,
        metavar="CODE",
        help="status code for a raw JSON payload (default: 200)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve = subparsers.add_parser(
        "serve",
        parents=[shared, raw],
        help="start the mock server",
        description="Start the mock server for a document or a raw JSON payload.",
    )
    serve.add_argument("source", help="path or URL to an OpenAPI, Swagger or JSON file")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    serve.add_argument(
        "--port",
        type=int,
        default=8000,
        help="bind port, 0 picks a free port (default: 8000)",
    )
    serve.add_argument(
        "--latency",
        type=int,
        default=0,
        metavar="MS",
        help="fixed response delay in milliseconds (default: 0)",
    )
    serve.add_argument(
        "--jitter",
        type=int,
        default=0,
        metavar="MS",
        help="extra random delay in milliseconds (default: 0)",
    )
    serve.add_argument(
        "--no-cors",
        action="store_true",
        help="do not send permissive CORS headers",
    )
    serve.add_argument(
        "--strict",
        action="store_true",
        help="return 400 when a required query or header parameter is missing",
    )
    serve.add_argument(
        "--quiet",
        action="store_true",
        help="do not log requests",
    )

    routes = subparsers.add_parser(
        "routes",
        parents=[shared, raw],
        help="list routes in a document",
        description="Print every route found in a document.",
    )
    routes.add_argument("source", help="path or URL to an OpenAPI, Swagger or JSON file")
    routes.add_argument("--json", action="store_true", help="print JSON instead of a table")

    sample = subparsers.add_parser(
        "sample",
        parents=[shared, raw],
        help="generate one sample response",
        description="Generate a single response body and print it to stdout.",
    )
    sample.add_argument("source", help="path or URL to an OpenAPI, Swagger or JSON file")
    sample.add_argument(
        "--index",
        type=int,
        default=0,
        metavar="N",
        help="route index to sample when --path is not given (default: 0)",
    )

    return parser


def _parse_method(method: str) -> str:
    value = (method or "GET").strip().lower()
    if not value:
        raise SpecError("method must not be empty")
    return value


def load_spec(args: argparse.Namespace, allow_raw: bool = True) -> Spec:
    text = read_source(args.source)
    document = parse_document(text, args.source)

    looks_like_spec = isinstance(document, dict) and (
        "openapi" in document or "swagger" in document
    )
    if looks_like_spec:
        return Spec(document, source=args.source, prefix=args.prefix)

    if not allow_raw:
        raise SpecError(
            "document has no openapi or swagger version field, so it cannot be served as a spec"
        )

    path = args.path if args.path is not None else "/"
    method = _parse_method(args.method)
    status = args.status if args.status is not None else 200
    if status < 100 or status > 599:
        raise SpecError("status must be between 100 and 599")
    return Spec.from_payload(
        document,
        source=args.source,
        path=path,
        method=method,
        status=status,
        prefix=args.prefix,
    )


def _faker_for(spec: Spec, args: argparse.Namespace) -> Faker:
    document = spec.document if spec.kind == "openapi" else None
    return Faker(
        document=document,
        seed=args.seed,
        include_optional=not args.no_optional,
        use_examples=not args.no_examples,
        max_depth=args.max_depth,
        null_probability=args.nulls,
    )


def _format_routes(routes: Sequence[Route]) -> str:
    if not routes:
        return "No routes."
    rows = [("METHOD", "PATH", "STATUS", "SUMMARY")]
    for route in routes:
        statuses = ",".join(str(code) for code in route.responses.keys())
        summary = route.summary or route.operation_id or ""
        rows.append((route.method.upper(), route.template, statuses, summary))
    widths = [max(len(row[index]) for row in rows) for index in range(3)]
    lines: List[str] = []
    for index, row in enumerate(rows):
        cells = [
            row[column].ljust(widths[column]) for column in range(3)
        ]
        line = "  ".join(cells) + "  " + row[3]
        lines.append(line.rstrip())
        if index == 0:
            lines.append("")
    return "\n".join(lines)


def _reject_path_for_spec(args: argparse.Namespace, spec: Spec) -> None:
    if args.path is not None and spec.kind != "json":
        raise SpecError(
            "--path only applies to raw JSON payloads, this document is a spec "
            "with its own paths"
        )


def _command_routes(args: argparse.Namespace) -> int:
    spec = load_spec(args)
    _reject_path_for_spec(args, spec)
    routes = list(spec.routes)
    if args.json:
        payload = {
            "source": spec.source,
            "kind": spec.kind,
            "format": spec.format,
            "title": spec.title,
            "version": spec.api_version,
            "count": len(routes),
            "routes": [route.to_dict() for route in routes],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(spec.describe())
    print(_format_routes(routes))
    return 0


def _pick_route(spec: Spec, args: argparse.Namespace):
    if args.path is not None:
        method = _parse_method(args.method)
        route, params, _ = spec.match(method, args.path)
        if route is None:
            route = spec.find(method, args.path)
            if route is not None:
                params = {name: "1" for name in route.param_names}
        if route is None:
            raise SpecError(f"no route for {method.upper()} {args.path}")
        return route, params
    if not spec.routes:
        raise SpecError("document has no routes to sample")
    if args.index < 0 or args.index >= len(spec.routes):
        raise SpecError(
            f"route index {args.index} is out of range, the document has {len(spec.routes)} routes"
        )
    route = spec.routes[args.index]
    return route, {name: "1" for name in route.param_names}


def _command_sample(args: argparse.Namespace) -> int:
    spec = load_spec(args)
    route, params = _pick_route(spec, args)
    status = args.status
    response: ResponseSpec = resolve_response(spec.version, route, status)
    options = ServerOptions(
        seed=args.seed,
        include_optional=not args.no_optional,
        use_examples=not args.no_examples,
        max_depth=args.max_depth,
        null_probability=args.nulls,
    )
    built = build_response(spec, route, response, args.seed, options, params)
    payload: Dict[str, Any] = {
        "request": f"{route.method.upper()} {route.template}",
        "status": built.status,
        "mediaType": built.media_type,
        "body": built.body,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _print_banner(spec: Spec, host: str, port: int, options: ServerOptions) -> None:
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"mog {__version__}")
    print(spec.describe())
    print(f"Listening on http://{display_host}:{port}")
    print(f"Routes: {len(spec.routes)}  seed: {options.seed}")
    print("Control: GET /__mog/health, GET /__mog/routes, POST /__mog/reset")
    print("Press Ctrl+C to stop.")


def _command_serve(args: argparse.Namespace) -> int:
    if args.port < 0 or args.port > 65535:
        raise SpecError("port must be between 0 and 65535")
    if args.latency < 0:
        raise SpecError("latency must not be negative")
    if args.jitter < 0:
        raise SpecError("jitter must not be negative")
    if args.max_depth < 0:
        raise SpecError("max depth must not be negative")

    spec = load_spec(args)
    _reject_path_for_spec(args, spec)
    options = ServerOptions(
        seed=args.seed,
        latency_ms=args.latency,
        jitter_ms=args.jitter,
        cors=not args.no_cors,
        strict=args.strict,
        include_optional=not args.no_optional,
        use_examples=not args.no_examples,
        max_depth=args.max_depth,
        null_probability=args.nulls,
        log_requests=not args.quiet,
    )

    if options.log_requests:
        logger = logging.getLogger("mog.request")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.propagate = False

    try:
        server = create_server(spec, host=args.host, port=args.port, options=options)
    except OSError as exc:
        raise SpecError(f"could not bind {args.host}:{args.port}: {exc}") from exc

    actual_port = server.server_address[1]
    _print_banner(spec, args.host, actual_port, options)
    sys.stdout.flush()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "serve":
            return _command_serve(args)
        if args.command == "routes":
            return _command_routes(args)
        if args.command == "sample":
            return _command_sample(args)
    except SpecError as exc:
        print(f"mog: error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
