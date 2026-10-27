import json
from dataclasses import fields
from datetime import datetime
from functools import wraps
from pathlib import Path

import pytest
from hypothesis import settings
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from dogana.wire import Wire, _registry

SCHEMAS = Path(__file__).parents[1] / "docs" / "wire"

settings.register_profile("deterministic", derandomize=True, database=None, deadline=None)
settings.load_profile("deterministic")


@pytest.fixture(scope="session")
def wire_validators():
    schemas = [json.loads(path.read_text()) for path in sorted(SCHEMAS.glob("*.schema.json"))]
    for schema in schemas:
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)
    assert len({schema["$id"] for schema in schemas}) == len(schemas)
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    checker = FormatChecker(formats=["date"])

    @checker.checks("date-time", raises=ValueError)
    def aware_datetime(value):
        return not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is not None

    validators = {}
    for schema in schemas:
        if "properties" not in schema:
            continue
        kind = schema["properties"]["kind"]["const"]
        assert kind not in validators
        validators[kind] = Draft202012Validator(schema, registry=registry, format_checker=checker)
    assert set(validators) == set(_registry())
    for kind, cls in _registry().items():
        schema = validators[kind].schema
        names = {field.name for field in fields(cls._wire_schema())} | {"kind", "version"}
        assert set(schema["properties"]) == set(schema["required"]) == names, kind
        assert schema["additionalProperties"] is False, kind
        assert schema["properties"]["version"] == {"type": "integer", "const": Wire.version}, kind
    return validators


@pytest.fixture(scope="session")
def validate_wire(wire_validators):
    def validate(value):
        kind = value.get("kind") if isinstance(value, dict) else None
        if not isinstance(kind, str) or kind not in wire_validators:
            raise ValidationError("unknown wire kind")
        wire_validators[kind].validate(value)

    return validate


@pytest.fixture(scope="session", autouse=True)
def validate_wire_outputs(validate_wire):
    original = Wire.to_dict

    @wraps(original)
    def validated(self):
        value = original(self)
        validate_wire(value)
        return value

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Wire, "to_dict", validated)
        yield
