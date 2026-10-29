# MOG

Mock OpenAPI Generator. A command line tool that reads an OpenAPI 3 document, a Swagger 2 document, or a plain JSON payload and starts a small HTTP server that answers requests with generated data.

No accounts, no config files, no database. One Python package with a standard library server.

```
$ mog serve examples/petstore.json
mog 0.1.0
petstore.json (OpenAPI 3.0.3, 6 routes)
Listening on http://127.0.0.1:8000
Routes: 6  seed: 0
Control: GET /__mog/health, GET /__mog/routes, POST /__mog/reset
Press Ctrl+C to stop.

$ curl -s localhost:8000/v1/pets/42
{"id":42,"name":"Priya Novak","species":"cat","status":"pending",
 "bornAt":"2026-05-18","tags":["steady","harbor"],
 "photoUrl":"https://api.example.com/users/271","weightKg":12.4,
 "owner":{"id":"9d0f...","email":"lena.tanaka@example.com","fullName":"Lena Tanaka"}}
```

## Install

Python 3.9 or newer.

```sh
pip install -e .
```

YAML documents need PyYAML:

```sh
pip install -e ".[yaml]"
```

The package installs a `mog` command. It also runs as a module:

```sh
python -m mog --help
```

## Commands

| Command | Purpose |
| --- | --- |
| `mog serve SOURCE` | Start the mock server for a document. |
| `mog routes SOURCE` | Print every route found in a document. |
| `mog sample SOURCE` | Print one generated response without starting a server. |

Every command accepts `--prefix PATH`, `--seed N`, `--no-optional`, `--no-examples`, `--max-depth N`, and `--nulls RATE`.

`serve` and `routes` also accept `--path`, `--method`, and `--status`. On a document that is not a spec, those flags describe the single raw route. On a spec they are rejected for `serve` and `routes`, because the document already defines its own paths.

## Inspecting a document

```sh
mog routes examples/petstore.json
```

```
petstore.json (OpenAPI 3.0.3, 6 routes)
METHOD  PATH              STATUS   SUMMARY

GET     /v1/pets          200,400  List pets
POST    /v1/pets          201      Create a pet
GET     /v1/pets/{petId}  200,404  Fetch one pet
DELETE  /v1/pets/{petId}  204      Delete one pet
GET     /v1/owners        200      List owners
GET     /v1/health        200      Liveness probe
```

Add `--json` for the same data as an object with `source`, `kind`, `format`, `title`, `version`, `count`, and `routes`. Each route carries its method, path, operation id, summary, tags, status codes, media types, parameters, and whether it is a raw payload.

`mog sample` prints one response:

```sh
mog sample examples/petstore.json --path /v1/pets/7 --seed 3
mog sample examples/petstore.json --index 1
mog sample examples/petstore.json --path /v1/pets --status 404
```

The output has `request`, `status`, `mediaType`, and `body`.

## Serving

```sh
mog serve examples/petstore.json --port 3000
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `8000` | Bind port. `0` picks a free port and prints it. |
| `--latency MS` | `0` | Fixed delay before each response. |
| `--jitter MS` | `0` | Extra seeded random delay up to this many milliseconds. |
| `--no-cors` | off | Stop sending permissive CORS headers. |
| `--strict` | off | Require every required query and header parameter. |
| `--quiet` | off | Do not log requests. |

The server uses `ThreadingHTTPServer`, so slow handlers do not block other requests. Responses include `X-Mog-Route` and `X-Mog-Seed`. When latency is applied, `X-Mog-Latency-Ms` is present too.

`OPTIONS` requests get a CORS preflight reply when CORS is on.

## Response selection

For a request that does not name a status code:

1. A literal `200` response wins.
2. Otherwise the lowest `2xx` response wins.
3. Otherwise `default` wins.

`204` and `304` responses are sent with no body and no `Content-Type`. Response level headers from the document are included, except hop by hop headers such as `Content-Type`, `Content-Length`, `Transfer-Encoding`, and `Connection`.

Requesting a status that the operation does not define produces a body that fits the code:

- `4xx` and `5xx` return `{"error": "simulated error", "status": N}`.
- Other codes return `{}`.
- `204` and `304` return nothing.

Media types are chosen from the document. JSON is the default when nothing better is available. Response bodies can be JSON, XML, form encoded, or plain text depending on the declared media type. XML is produced by walking the generated object.

## Raw JSON payloads

Point MOG at a JSON file that is not an OpenAPI or Swagger document and it serves the exact payload:

```sh
mog serve examples/payload.json --path /events --method POST --status 201
```

```
$ curl -s -X POST localhost:8000/events
[{"event":"invoice.paid","invoiceId":"inv_9f2a41", ...}]
```

The raw route reports the file name as its title and makes the inferred schema available to `mog sample` and `GET /__mog/spec`. The live route always returns the original bytes of the payload, not regenerated data. Path parameters in the route template are still matched, but the payload is served unchanged.

`--path` defaults to `/`, `--method` defaults to `GET`, and `--status` defaults to `200`.

## Fake data

Values come from a seeded generator that understands JSON Schema. Supported keywords include:

- `type`, `properties`, `required`, `additionalProperties`
- `items`, `minItems`, `maxItems`
- `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`
- `minLength`, `maxLength`, `pattern`, `format`, `enum`, `const`, `default`, `example`, `examples`
- `$ref`, `allOf`, `oneOf`, `anyOf`, `nullable`

Formats include `date-time`, `date`, `time`, `duration`, `email`, `hostname`, `ipv4`, `ipv6`, `uri`, `uuid`, `byte`, `binary`, `password`, and `phone`. Property names add hints: `email`, `first_name`, `city`, `country`, `company`, `price`, `token`, `sha256`, `slug`, `status`, `url`, and similar fields get values that look like what the name suggests.

Two runs with the same seed and the same document produce the same body. The seed is mixed with a counter that increases once per request, so consecutive responses differ unless you pin the seed.

| Option | Default | Meaning |
| --- | --- | --- |
| `--seed N` | `0` | Base seed for generation. |
| `--no-optional` | off | Emit only properties listed in `required`. |
| `--no-examples` | off | Ignore `example`, `examples`, and `default` values. |
| `--max-depth N` | `6` | Stop expanding nested objects beyond this depth. |
| `--nulls RATE` | `0` | Chance from `0` to `1` that a nullable field becomes `null`. |

Response examples declared in the document are reused as is, unless `--no-examples` is set. If a path parameter such as `petId` is present in the generated object under a matching key, or under `id`, its value is replaced with the value from the request.

## Per request overrides

Query parameters and headers can change a single response without restarting the server.

| Query | Header | Effect |
| --- | --- | --- |
| `__status=404` | `X-Mog-Status: 404` | Return this status code. |
| `__delay=250` | `X-Mog-Delay: 250` | Add this many milliseconds of delay. |
| `__seed=12` | `X-Mog-Seed: 12` | Use this seed for this request. |

Invalid values return `400` with a short message. Status must be between `100` and `599`, delay between `0` and `60000` milliseconds, and seed must be an integer.

## Control endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /__mog` | List the control endpoints. |
| `GET /__mog/health` | Status, version, uptime, request count, seed, route count, and document metadata. |
| `GET /__mog/routes` | Routes as JSON. Accepts `method` and `tag` filters. |
| `GET /__mog/spec` | The loaded document, or the payload plus inferred schema for raw JSON. |
| `GET /__mog/sample?path=/pets&method=GET` | Generate one response. Accepts `__status` and `__seed`. |
| `POST /__mog/reset` | Reset uptime, request count, and the seed counter. |

Control endpoints are not part of the mock spec. They are always available and always answer JSON.

## Strict parameters

With `--strict`, a request that omits a required `query` or `header` parameter returns `400`:

```json
{"error": "missing required parameter", "parameters": ["q"]}
```

Path parameters are not checked separately because the route only matches when they are present. Request bodies are read and discarded, not validated.

## YAML

YAML documents are parsed when PyYAML is installed. Without it, JSON is still supported and a YAML file produces a message that points at `pip install 'mog[yaml]'`.

The order of attempts is JSON first, YAML second. A file that starts with a UTF-8 byte order mark is still parsed.

## Python API

```python
from mog import Spec, create_server

spec = Spec.from_payload([{"id": 1, "name": "beacon"}], path="/events", method="POST")
server = create_server(spec, port=0)
print(server.server_address[1])
server.serve_forever()
```

| Name | Role |
| --- | --- |
| `Spec` | Loaded document with `routes`, `match`, `find`, and `describe`. |
| `Spec.from_payload` | Wrap a JSON value as a single route. |
| `Route` | Method, template, regex, parameters, responses, and `to_dict`. |
| `resolve_response` | Pick a response for a route and optional status code. |
| `Faker` | Seeded generator with `generate(schema, name)`. |
| `infer_schema` | Build a JSON Schema from a sample value. |
| `create_server` | Build a `ThreadingHTTPServer` with the mock handler. |

`Spec.match(method, path)` returns `(route, params, allowed)`. `allowed` lists the methods that do match the path, which is what drives the `405` reply.

## Limits

- External `$ref` values are not fetched. Only local references such as `#/components/schemas/Pet` are resolved.
- Request bodies are not validated against `requestBody`.
- `link` and `callback` objects are ignored.
- Response examples with `$ref` are not expanded by the example loader, but schemas are.
- Large specs are matched with regular expressions per request. This is fine for local testing, not for load testing a spec with thousands of paths.
- The server is for local development. It has no authentication and binds to `127.0.0.1` by default.

## Tests

```sh
python -m unittest discover -s tests
```

The suite covers document loading, references, path templates, route precedence, response selection, raw payloads, schema inference, constraints, formats, seeded determinism, the live HTTP server, headers, status overrides, CORS, delays, strict parameters, control endpoints, CLI output, and exit codes.

## License

MIT. See `LICENSE`.
