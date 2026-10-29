"""Seeded fake data generation from JSON Schema and schema inference."""

from __future__ import annotations

import base64
import copy
import math
import re
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from .util import SpecError, resolve_ref

_FIRST_NAMES = (
    "Alex",
    "Priya",
    "Mateo",
    "Nora",
    "Kenji",
    "Sofia",
    "Amir",
    "Lena",
    "Diego",
    "Hana",
    "Owen",
    "Zara",
)
_LAST_NAMES = (
    "Chen",
    "Patel",
    "Garcia",
    "Novak",
    "Tanaka",
    "Rossi",
    "Haddad",
    "Fischer",
    "Silva",
    "Kim",
    "Bennett",
    "Okafor",
)
_CITIES = ("Lisbon", "Osaka", "Toronto", "Nairobi", "Lyon", "Austin", "Tallinn", "Medellin")
_COUNTRIES = ("United States", "Canada", "Germany", "Japan", "Brazil", "Kenya", "Portugal")
_COMPANIES = ("Northwind Labs", "Kestrel Systems", "Blue Harbor", "Atlas Works", "Juniper Data")
_COLORS = ("teal", "amber", "crimson", "indigo", "slate", "olive")
_STREETS = ("Maple", "Cedar", "Harbor", "Foundry", "Juniper", "Orchard")
_WORDS = (
    "adaptive",
    "service",
    "signal",
    "harbor",
    "runtime",
    "cluster",
    "packet",
    "steady",
    "vector",
    "beacon",
    "ledger",
    "orbit",
    "canvas",
    "ember",
    "quiet",
    "hollow",
    "summit",
    "render",
    "field",
    "drift",
    "token",
    "margin",
    "copper",
    "willow",
)
_SEGMENTS = ("users", "orders", "products", "invoices", "events", "reports")
_DOMAINS = ("example.com", "example.org", "mail.test")

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"
_WORD_CHARS = _ALPHABET + _ALPHABET.upper() + _DIGITS + "_"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(:\d+)?$")


def _expand_class(body: str) -> Optional[str]:
    if body.startswith("^"):
        return None
    chars: List[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            token = body[index + 1]
            if token == "d":
                chars.extend(_DIGITS)
            elif token == "w":
                chars.extend(_WORD_CHARS)
            elif token == "s":
                chars.append(" ")
            else:
                chars.append(token)
            index += 2
            continue
        if index + 2 < len(body) and body[index + 1] == "-":
            start, end = body[index], body[index + 2]
            if ord(start) <= ord(end) and ord(end) - ord(start) <= 128:
                chars.extend(chr(code) for code in range(ord(start), ord(end) + 1))
                index += 3
                continue
            return None
        chars.append(char)
        index += 1
    unique: List[str] = []
    seen = set()
    for char in chars:
        if char not in seen:
            seen.add(char)
            unique.append(char)
    return "".join(unique) or None


def pattern_sample(pattern: str, rng: Any) -> Optional[str]:
    """Produce one string for a small regular expression subset.

    Supported pieces are literals, escaped literals, ``.``, character classes,
    ``\\d``, ``\\w``, ``\\s`` and the quantifiers ``?``, ``*``, ``+``,
    ``{n}`` and ``{n,m}``. Groups and alternation return None.
    """

    text = pattern
    if text.startswith("^"):
        text = text[1:]
    if text.endswith("$"):
        text = text[:-1]
    if not text:
        return ""
    output: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "()|":
            return None
        chars: Optional[str]
        literal: Optional[str]
        if char == "[":
            end = text.find("]", index + 1)
            if end < 0:
                return None
            chars = _expand_class(text[index + 1 : end])
            if not chars:
                return None
            literal = None
            index = end + 1
        elif char == "\\":
            if index + 1 >= len(text):
                return None
            token = text[index + 1]
            if token == "d":
                chars = _DIGITS
                literal = None
            elif token == "w":
                chars = _WORD_CHARS
                literal = None
            elif token == "s":
                chars = " "
                literal = None
            elif token in "nrt":
                return None
            else:
                chars = None
                literal = token
            index += 2
        elif char == ".":
            chars = _WORD_CHARS
            literal = None
            index += 1
        elif char in "?*+{":
            return None
        else:
            chars = None
            literal = char
            index += 1

        minimum, maximum = 1, 1
        if index < len(text):
            quantifier = text[index]
            if quantifier == "?":
                minimum, maximum = 0, 1
                index += 1
            elif quantifier == "*":
                minimum, maximum = 0, 4
                index += 1
            elif quantifier == "+":
                minimum, maximum = 1, 4
                index += 1
            elif quantifier == "{":
                end = text.find("}", index)
                if end < 0:
                    return None
                spec = text[index + 1 : end]
                if not spec:
                    return None
                try:
                    if "," in spec:
                        low, high = spec.split(",", 1)
                        minimum = int(low) if low else 0
                        maximum = int(high) if high else max(minimum, 4)
                    else:
                        minimum = maximum = int(spec)
                except ValueError:
                    return None
                index = end + 1
        if minimum < 0 or maximum < minimum or maximum > 32:
            return None
        count = rng.randint(minimum, maximum)
        for _ in range(count):
            output.append(rng.choice(chars) if chars else (literal or ""))
    return "".join(output)


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    output = dict(base)
    for key, value in extra.items():
        if key == "allOf":
            continue
        if key == "required" and isinstance(value, list):
            existing = output.get("required")
            existing = existing if isinstance(existing, list) else []
            merged = list(existing)
            for item in value:
                if item not in merged:
                    merged.append(item)
            output["required"] = merged
        elif key == "properties" and isinstance(value, dict):
            properties = dict(output.get("properties") or {})
            properties.update(value)
            output["properties"] = properties
        else:
            output[key] = value
    return output


def _merge_inferred(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    if not left:
        return dict(right)
    if not right:
        return dict(left)
    if left == right:
        return dict(left)
    left_type = left.get("type")
    right_type = right.get("type")
    if left_type == right_type == "object":
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for schema in (left, right):
            for key, sub in (schema.get("properties") or {}).items():
                if key in properties:
                    properties[key] = _merge_inferred(properties[key], sub)
                else:
                    properties[key] = sub
                if key not in required:
                    required.append(key)
        return {"type": "object", "properties": properties, "required": required}
    if left_type == right_type == "array":
        return {
            "type": "array",
            "items": _merge_inferred(left.get("items") or {}, right.get("items") or {}),
        }
    if left_type == right_type:
        merged = dict(left)
        for key in ("format", "nullable"):
            if key not in merged and key in right:
                merged[key] = right[key]
        return merged
    return {"anyOf": [left, right]}


def _detect_format(value: str) -> Optional[str]:
    if _UUID_RE.match(value):
        return "uuid"
    if _DATETIME_RE.match(value):
        return "date-time"
    if _DATE_RE.match(value):
        return "date"
    if _EMAIL_RE.match(value):
        return "email"
    if _IPV4_RE.match(value):
        return "ipv4"
    if _URI_RE.match(value):
        return "uri"
    if _HOST_RE.match(value):
        return "hostname"
    return None


def infer_schema(value: Any, depth: int = 0, max_depth: int = 8) -> Dict[str, Any]:
    if depth > max_depth:
        return {}
    if value is None:
        return {"nullable": True}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        schema: Dict[str, Any] = {"type": "string"}
        detected = _detect_format(value)
        if detected:
            schema["format"] = detected
        return schema
    if isinstance(value, (list, tuple)):
        if not value:
            return {"type": "array", "items": {}}
        items = infer_schema(value[0], depth + 1, max_depth)
        for entry in value[1:20]:
            items = _merge_inferred(items, infer_schema(entry, depth + 1, max_depth))
        return {"type": "array", "items": items}
    if isinstance(value, dict):
        properties = {
            str(key): infer_schema(sub, depth + 1, max_depth)
            for key, sub in value.items()
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties.keys()),
        }
    return {"type": "string"}


class Faker:
    """Deterministic JSON value generator driven by a seed and a schema."""

    def __init__(
        self,
        document: Any = None,
        seed: int = 0,
        include_optional: bool = True,
        use_examples: bool = True,
        max_depth: int = 6,
        null_probability: float = 0.0,
    ) -> None:
        import random

        self.document = document
        self.rng = random.Random(seed)
        self.seed = seed
        self.include_optional = include_optional
        self.use_examples = use_examples
        self.max_depth = max(0, int(max_depth))
        self.null_probability = max(0.0, min(1.0, float(null_probability)))

    def generate(self, schema: Any, name: str = "") -> Any:
        return self._generate(schema, str(name or ""), 0, frozenset())

    def _deref(self, ref: str) -> Dict[str, Any]:
        if self.document is None:
            return {}
        try:
            target = resolve_ref(self.document, ref)
        except SpecError:
            return {}
        return target if isinstance(target, dict) else {}

    def _flatten_all_of(
        self, schema: Dict[str, Any], seen: Optional[set] = None, depth: int = 0
    ) -> Dict[str, Any]:
        if depth > 24:
            return {}
        seen = set(seen or ())
        current = schema
        for _ in range(24):
            ref = current.get("$ref")
            if not ref:
                break
            if ref in seen:
                return {}
            seen.add(ref)
            current = self._deref(str(ref))
            if not current:
                return {}
        merged = {key: value for key, value in current.items() if key != "allOf"}
        parts = current.get("allOf")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    merged = _deep_merge(
                        merged, self._flatten_all_of(part, seen, depth + 1)
                    )
        return merged

    def _generate(
        self, schema: Any, name: str, depth: int, stack: frozenset
    ) -> Any:
        if depth > self.max_depth:
            return self._minimal(schema)
        if schema is True:
            schema = {}
        if schema is False:
            return None
        if not isinstance(schema, dict):
            return "value"

        ref = schema.get("$ref")
        if isinstance(ref, str):
            if ref in stack:
                return self._minimal(self._deref(ref))
            target = self._deref(ref)
            if not target:
                return self._minimal({})
            merged = dict(target)
            for key, value in schema.items():
                if key != "$ref":
                    merged[key] = value
            return self._generate(merged, name, depth, stack | {ref})

        if "allOf" in schema:
            schema = self._flatten_all_of(schema)

        if "const" in schema:
            return copy.deepcopy(schema["const"])

        if "oneOf" in schema or "anyOf" in schema:
            key = "oneOf" if "oneOf" in schema else "anyOf"
            variants = schema.get(key)
            if isinstance(variants, list):
                choices = [entry for entry in variants if isinstance(entry, dict)]
                if choices:
                    return self._generate(
                        self.rng.choice(choices), name, depth, stack
                    )

        if self.use_examples and "example" in schema:
            return copy.deepcopy(schema["example"])

        if self.use_examples and "examples" in schema:
            examples = schema["examples"]
            if isinstance(examples, list) and examples:
                return copy.deepcopy(examples[0])
            if isinstance(examples, dict) and examples:
                first = next(iter(examples.values()))
                if isinstance(first, dict) and "value" in first:
                    first = first["value"]
                return copy.deepcopy(first)

        if self.use_examples and "default" in schema:
            return copy.deepcopy(schema["default"])

        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return copy.deepcopy(self.rng.choice(enum))

        schema_type = self._schema_type(schema)
        if schema_type == "null":
            return None
        nullable = schema.get("nullable") is True
        if nullable and self.null_probability > 0:
            if self.rng.random() < self.null_probability:
                return None

        if schema_type == "object":
            return self._object(schema, depth, stack)
        if schema_type == "array":
            return self._array(schema, name, depth, stack)
        if schema_type == "string":
            return self._string(schema, name)
        if schema_type == "integer":
            return self._number(schema, name, integer=True)
        if schema_type == "number":
            return self._number(schema, name, integer=False)
        if schema_type == "boolean":
            return self.rng.random() < 0.5

        hint = self._by_name(name)
        if hint is not None:
            return hint
        return "value"

    def _schema_type(self, schema: Dict[str, Any]) -> Optional[str]:
        value = schema.get("type")
        if isinstance(value, list):
            candidates = [str(item) for item in value if item != "null"]
            return candidates[0] if candidates else "null"
        if isinstance(value, str):
            return value.lower()
        if "properties" in schema or "additionalProperties" in schema:
            return "object"
        if "items" in schema:
            return "array"
        return None

    def _minimal(self, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return ""
        if "$ref" in schema:
            schema = self._deref(str(schema["$ref"]))
        schema_type = self._schema_type(schema)
        if schema_type == "object":
            return {}
        if schema_type == "array":
            return []
        if schema_type in ("integer", "number"):
            return 0
        if schema_type == "boolean":
            return False
        if schema_type == "null":
            return None
        if schema_type is None:
            return {}
        return ""

    def _object(self, schema: Dict[str, Any], depth: int, stack: frozenset) -> Dict[str, Any]:
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required_names = set(required) if isinstance(required, list) else set()
        output: Dict[str, Any] = {}
        if properties:
            for key, sub in properties.items():
                if (
                    not self.include_optional
                    and required_names
                    and key not in required_names
                ):
                    continue
                output[key] = self._generate(
                    sub, str(key), depth + 1, stack
                )
        else:
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                for index in range(2):
                    key = f"key{index + 1}"
                    output[key] = self._generate(
                        additional, key, depth + 1, stack
                    )
        return output

    def _array(
        self, schema: Dict[str, Any], name: str, depth: int, stack: frozenset
    ) -> List[Any]:
        items = schema.get("items")
        if isinstance(items, list):
            items = items[0] if items else {}
        if not isinstance(items, dict):
            items = {}
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        low = int(minimum) if isinstance(minimum, int) else 1
        high = int(maximum) if isinstance(maximum, int) else 3
        low = max(0, min(low, 20))
        high = max(low, min(high, 20))
        count = self.rng.randint(low, high)
        return [
            self._generate(items, name, depth + 1, stack) for _ in range(count)
        ]

    def _string(self, schema: Dict[str, Any], name: str) -> str:
        value: Optional[str] = None
        fmt = schema.get("format")
        if isinstance(fmt, str) and fmt:
            value = self._by_format(fmt.lower())
        if value is None and isinstance(schema.get("pattern"), str):
            value = pattern_sample(schema["pattern"], self.rng)
        if value is None:
            value = self._by_name(name)
        if value is None:
            value = self._sentence(3)
        return self._apply_string_limits(value, schema)

    def _apply_string_limits(self, value: str, schema: Dict[str, Any]) -> str:
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and maximum >= 0 and len(value) > maximum:
            value = value[:maximum]
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            padding = "".join(
                self.rng.choice(_ALPHABET) for _ in range(minimum - len(value))
            )
            value = value + padding
        return value

    def _number(self, schema: Dict[str, Any], name: str, integer: bool) -> Any:
        hint = self._numeric_hint(name)
        default_low, default_high = 1.0, 1000.0
        if integer:
            default_low, default_high = 1.0, 10000.0
        if hint == "id":
            default_low, default_high = 1.0, 10000.0
        elif hint == "money":
            default_low, default_high = 1.0, 999.0
        elif hint == "count":
            default_low, default_high = 0.0, 100.0
        elif hint == "age":
            default_low, default_high = 18.0, 80.0
        elif hint == "latitude":
            default_low, default_high = -90.0, 90.0
        elif hint == "longitude":
            default_low, default_high = -180.0, 180.0

        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        min_exclusive = False
        max_exclusive = False
        exclusive_min = schema.get("exclusiveMinimum")
        if isinstance(exclusive_min, bool):
            min_exclusive = exclusive_min
        elif isinstance(exclusive_min, (int, float)):
            minimum = exclusive_min
            min_exclusive = True
        exclusive_max = schema.get("exclusiveMaximum")
        if isinstance(exclusive_max, bool):
            max_exclusive = exclusive_max
        elif isinstance(exclusive_max, (int, float)):
            maximum = exclusive_max
            max_exclusive = True

        low = float(minimum) if isinstance(minimum, (int, float)) else default_low
        high = float(maximum) if isinstance(maximum, (int, float)) else default_high
        if high < low:
            high = low

        multiple = schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and multiple <= 0:
            multiple = None

        if integer:
            low_int = math.ceil(low) + (1 if min_exclusive else 0)
            high_int = math.floor(high) - (1 if max_exclusive else 0)
            if low_int > high_int:
                low_int = high_int = int(round(low))
            value = self.rng.randint(low_int, high_int)
            if isinstance(multiple, (int, float)):
                adjusted = int(math.ceil(value / multiple) * multiple)
                if adjusted > high_int:
                    adjusted = int(math.floor(high_int / multiple) * multiple)
                value = max(low_int, min(high_int, adjusted))
            return value

        low_value = low + (1e-6 if min_exclusive else 0.0)
        high_value = high - (1e-6 if max_exclusive else 0.0)
        if high_value < low_value:
            low_value = high_value = low
        value = self.rng.uniform(low_value, high_value)
        if isinstance(multiple, (int, float)):
            value = round(value / multiple) * multiple
        digits = 2 if hint == "money" else 4
        value = round(value, digits)
        if value < low:
            value = low
        if value > high:
            value = high
        return value

    def _numeric_hint(self, name: str) -> Optional[str]:
        raw = str(name or "").strip()
        key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
        if key == "id" or key.endswith("_id") or raw.endswith("Id") or raw.endswith("ID"):
            return "id"
        if "latitude" in key or key == "lat":
            return "latitude"
        if "longitude" in key or key in ("lng", "lon"):
            return "longitude"
        if any(word in key for word in ("price", "amount", "cost", "balance", "salary")):
            return "money"
        if key == "age":
            return "age"
        if key in ("count", "total", "quantity", "qty") or key.endswith("_count"):
            return "count"
        return None

    def _by_format(self, fmt: str) -> Optional[str]:
        if fmt == "date-time":
            return self._datetime()
        if fmt == "date":
            return self._date()
        if fmt == "time":
            return f"{self.rng.randint(0, 23):02d}:{self.rng.randint(0, 59):02d}:{self.rng.randint(0, 59):02d}Z"
        if fmt == "duration":
            return "PT1H30M"
        if fmt in ("email", "idn-email"):
            return self._email()
        if fmt in ("hostname", "idn-hostname"):
            return f"app-{self.rng.randint(1, 99)}.example.com"
        if fmt == "ipv4":
            return f"10.{self.rng.randint(0, 255)}.{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}"
        if fmt == "ipv6":
            return f"2001:db8::{self.rng.randint(1, 65535):x}"
        if fmt in ("uri", "url", "uri-reference"):
            return f"https://api.example.com/{self.rng.choice(_SEGMENTS)}/{self.rng.randint(1, 999)}"
        if fmt == "uuid":
            return self._uuid()
        if fmt == "byte":
            raw = bytes(self.rng.randrange(256) for _ in range(12))
            return base64.b64encode(raw).decode("ascii")
        if fmt == "binary":
            return "binary"
        if fmt == "password":
            return "s3cret-Passw0rd"
        if fmt == "phone":
            return f"+1-555-{self.rng.randint(1000, 9999)}"
        if fmt == "int32":
            return str(self.rng.randint(1, 2**31 - 1))
        if fmt == "int64":
            return str(self.rng.randint(1, 10**12))
        if fmt in ("float", "double"):
            return str(round(self.rng.uniform(0, 1000), 3))
        return None

    def _by_name(self, name: str) -> Optional[str]:
        raw = str(name or "").strip()
        if not raw:
            return None
        key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
        if not key:
            return None

        if "uuid" in key or key == "guid":
            return self._uuid()
        if key == "id" or key.endswith("_id") or raw.endswith("Id") or raw.endswith("ID"):
            return str(self.rng.randint(1, 10000))
        if key.endswith("_at") or raw.endswith("At"):
            return self._datetime()
        if key.endswith("_date") or key in ("date", "birthday", "birth_date", "due_date"):
            return self._date()
        if "email" in key:
            return self._email()
        if "phone" in key or "mobile" in key or "telephone" in key:
            return f"+1-555-{self.rng.randint(1000, 9999)}"
        if key in ("first_name", "given_name", "forename"):
            return self._first_name()
        if key in ("last_name", "family_name", "surname"):
            return self._last_name()
        if key in ("username", "user_name", "handle", "login"):
            return (
                self._first_name().lower()
                + self._last_name()[0].lower()
                + str(self.rng.randint(10, 99))
            )
        if key in ("company", "company_name", "organization", "organisation", "employer"):
            return self.rng.choice(_COMPANIES)
        if key in ("name", "full_name", "display_name", "person_name"):
            return f"{self._first_name()} {self._last_name()}"
        if "latitude" in key or key == "lat":
            return f"{self.rng.uniform(-90, 90):.4f}"
        if "longitude" in key or key in ("lng", "lon"):
            return f"{self.rng.uniform(-180, 180):.4f}"
        if "token" in key or "secret" in key or "api_key" in key or "apikey" in key:
            return "".join(self.rng.choice("0123456789abcdef") for _ in range(32))
        if "sha256" in key or key == "hash" or key.endswith("_hash"):
            return "".join(self.rng.choice("0123456789abcdef") for _ in range(64))
        if "md5" in key:
            return "".join(self.rng.choice("0123456789abcdef") for _ in range(32))
        if key == "slug" or key.endswith("_slug"):
            return self._slug()
        if key == "version" or key.endswith("_version"):
            return f"{self.rng.randint(0, 4)}.{self.rng.randint(0, 12)}.{self.rng.randint(0, 9)}"
        if key in ("status", "state", "phase"):
            return "active"
        if "currency" in key:
            return "USD"
        if key.endswith("country_code") or key == "country_code":
            return "US"
        if "country" in key:
            return self.rng.choice(_COUNTRIES)
        if "city" in key or key == "town":
            return self.rng.choice(_CITIES)
        if "postal" in key or "zip" in key:
            return f"{self.rng.randint(10000, 99999)}"
        if "street" in key:
            return f"{self.rng.randint(1, 999)} {self.rng.choice(_STREETS)} St"
        if "address" in key:
            return (
                f"{self.rng.randint(1, 999)} {self.rng.choice(_STREETS)} St, "
                f"{self.rng.choice(_CITIES)}"
            )
        if "language" in key or "locale" in key:
            return "en"
        if "timezone" in key or key == "tz":
            return "UTC"
        if "mime" in key or "content_type" in key:
            return "application/json"
        if "filename" in key or "file_name" in key:
            return "report-2026.pdf"
        if key in ("path", "file_path", "filepath"):
            return "/var/data/report.pdf"
        if "color" in key or "colour" in key:
            return self.rng.choice(_COLORS)
        if key in ("image", "avatar", "photo", "thumbnail", "picture", "cover"):
            return f"https://example.com/images/{self.rng.randint(1, 999)}.jpg"
        if key.endswith("_url") or key.endswith("_uri") or key.endswith("_href"):
            return f"https://example.com/{self.rng.choice(_SEGMENTS)}/{self.rng.randint(1, 999)}"
        if key in ("url", "uri", "link", "website", "homepage"):
            return f"https://example.com/{self.rng.choice(_SEGMENTS)}/{self.rng.randint(1, 999)}"
        if key in ("description", "summary", "bio", "about"):
            return self._sentence(8)
        if key in ("title", "subject", "headline"):
            return self._title()
        if key.endswith("_name"):
            return f"{self._first_name()} {self._last_name()}"
        return None

    def _first_name(self) -> str:
        return self.rng.choice(_FIRST_NAMES)

    def _last_name(self) -> str:
        return self.rng.choice(_LAST_NAMES)

    def _email(self) -> str:
        first = self._first_name().lower()
        last = self._last_name().lower()
        return f"{first}.{last}@{self.rng.choice(_DOMAINS)}"

    def _uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def _date(self) -> str:
        start = date(2026, 1, 1)
        return (start + timedelta(days=self.rng.randint(0, 364))).isoformat()

    def _datetime(self) -> str:
        day = self._date()
        return (
            f"{day}T{self.rng.randint(0, 23):02d}:"
            f"{self.rng.randint(0, 59):02d}:{self.rng.randint(0, 59):02d}Z"
        )

    def _sentence(self, count: int) -> str:
        words = [self.rng.choice(_WORDS) for _ in range(max(1, count))]
        words[0] = words[0].capitalize()
        return " ".join(words) + "."

    def _title(self) -> str:
        words = [self.rng.choice(_WORDS).capitalize() for _ in range(3)]
        return " ".join(words)

    def _slug(self) -> str:
        words = [self.rng.choice(_WORDS) for _ in range(3)]
        return "-".join(words)
