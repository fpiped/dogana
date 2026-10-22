# Wire schemas

Each registered wire kind has a JSON Schema 2020-12 document named after its class in
snake case. `types.schema.json` defines shared scalars, tags, windows and key lists.
Schema IDs use `urn:dogana:wire:1:<ClassName>`; the shared definitions use
`urn:dogana:wire:1:types`. Load all files into a local schema registry before validation.
Enable `date` and `date-time` format checks.

Datetime tags accept timezone-aware RFC 3339 values with `Z` or a numeric offset and
up to six fractional digits. Public serialization normalizes these values to UTC.

All version 1 fields are required, including nulls and defaults. Structured values and
tagged scalars reject unknown fields. The `Contract.shapes` mapping accepts nonempty
names, with each value checked against the Shape schema. `Contract.types` maps only key,
window and cursor columns to logical types; `schema_hash` references the external data contract. Receipt carries only `outcome`
(`accepted`, `rejected` or `replayed`), with a transition for acceptance and replay.

These schemas specify the wire structure, scalar domains and local constraints.
Runtime validation also checks relationships such as endpoint order, region partitioning,
contract bindings, publication identity and exact binary64 number representation.
Identity documents omit wire fields and are outside these schemas.

The pytest `validate_wire(value)` fixture validates raw decoded JSON. The session fixture
also validates every successful public `Wire.to_dict()` result, including calls through
`to_json()`, and checks schemas against the registered kinds and their public wire fields.
Nested values are validated through schema references. Subprocesses, collection-time calls,
direct `encode()` or `_document()` calls, and identity forms bypass that instrumentation.
