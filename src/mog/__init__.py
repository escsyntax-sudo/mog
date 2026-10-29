"""MOG: mock server for OpenAPI, Swagger and raw JSON payloads."""

__version__ = "0.1.0"

from .generator import Faker, infer_schema
from .spec import Route, Spec, SpecError, resolve_response

__all__ = [
    "Faker",
    "Route",
    "Spec",
    "SpecError",
    "infer_schema",
    "resolve_response",
    "__version__",
]
