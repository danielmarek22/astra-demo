# Gmail 2.3.3 schema compatibility regression

`catalog.json` is the actual 15-tool catalog returned on **2026-11-04** by the locally
installed **`@mcp-z/mcp-gmail@2.3.3`**, parsed by the official MCP Python SDK 2.3.0.
The SDK's outer tool fields use snake_case; nested JSON Schemas are unchanged.
The capture records dependency versions and the installed dependency-lock SHA-256.
It contains public tool definitions, not account or message data.

Capture used the installed CLI with Node **v24.22.2**, `--headless`, a temporary
working directory containing an empty `.mcp.json`, a dummy Google client ID and
`TOKEN_STORE_URI=memory://`. It negotiated **2026-07-28** and performed only connection
and `tools/list`. No tool was invoked, no real credentials/token store were used,
and the child was closed afterward. Nothing was installed or changed in the user's
Gmail server directory. The accompanying MIT license is from that package.

The package is published at [npm](https://www.npmjs.com/package/@mcp-z/mcp-gmail)
and its source at [mcp-z/mcp-gmail](https://github.com/mcp-z/mcp-gmail).

## Confirmed rejection

Exactly **two** definitions failed the original Astra schema admission check:
`message-search` and `messages-export-csv`, both on their input schema. All other
13 tools passed both input/output checks. These two tools share this structure:

```json
{
  "properties": {
    "query": {
      "anyOf": [{"$ref": "#/$defs/__schema0"}, {"type": "string", "minLength": 1}]
    }
  },
  "$defs": {
    "__schema0": {
      "type": "object",
      "properties": {
        "$and": {"type": "array", "items": {"$ref": "#/$defs/__schema0"}},
        "$or": {"type": "array", "items": {"$ref": "#/$defs/__schema0"}},
        "$not": {"$ref": "#/$defs/__schema0"}
      }
    }
  }
}
```

This excerpt omits unrelated fields only for explanation. Tests use the **complete
captured definitions**, not this excerpt. The first actual rejection occurs at
`input_schema#/$defs/__schema0/properties/$and/items/$ref`: the old validator finds
the reference already active and raises `References must be acyclic local JSON pointers`.
Equivalent recursive references exist under `$or/items` and `$not`.

After fixing recursion alone, the old blanket regex ban would reject two more
locations in each affected input schema:

```text
input_schema#/$defs/__schema0/properties/date/properties/$gte/pattern
input_schema#/$defs/__schema0/properties/date/properties/$lt/pattern
```

Their exact value is `^\d{4}-\d{2}-\d{2}$`. These are valid JSON Schema 2020-12
features supported by the existing jsonschema dependency. This was an Astra subset
policy rejection, not a malformed Gmail schema, failed connection or OAuth error.

## Focused compatibility policy

- Allow local input-schema recursion only when a cycle descends through instance
  structure, such as an object property or array item. Same-instance cycles stay
  rejected. Output schemas remain acyclic because the SDK validates output itself.
- Accept exactly the fixed date regex above and evaluate it unchanged with the
  existing validator. Arbitrary regexes and `patternProperties` remain rejected.
  This tests the advertised string shape; it adds no calendar-validity restriction.
- Preserve schemas, `$defs`/`$ref`, constraints, descriptions and fingerprints as
  received. No flattening, unrolling, constraint stripping, default insertion or
  argument coercion occurs. Numeric strings still fail an integer-only schema even
  if its description claims otherwise.
- Keep object-root, dialect, schema-byte/depth/node and reference-expansion bounds.
  External/dynamic references and custom `$id` remain rejected. Nested dialect
  declarations are explicitly rejected to prevent recursive validation from
  switching to an unbounded validator. An empty referencing registry forbids fetching.
- Before MCP approval/dispatch, bound finite arguments to an absolute 64 KiB ceiling,
  depth 32 and 8192 nodes, then limit schema validation to 16384 keyword evaluations.
  Each call gets its own counter and delegates every constraint to Draft 2020-12.
  The binding's configured byte limit still applies before approval/remote invocation.
  Native argument validation retains its existing behavior.
- Preserve manual enablement, frozen authority/catalogs, approvals and contract
  preflight. Admission does not enable a tool. Previously rejected or changed
  contracts still require explicit manual review.
- Management returns application-owned diagnostic reasons and JSON-pointer paths.
  Credential values are redacted before path escaping/truncation. Raw SDK exception
  bodies, rejected regex values and reference targets are not displayed. The existing
  MCP UI renders the reason as text and keeps rejected controls disabled.

The official [JSON Schema recursive-reference documentation](https://json-schema.org/understanding-json-schema/structuring#recursion)
explains recursive schemas; jsonschema documents
[delegating to existing keyword validators](https://python-jsonschema.readthedocs.io/en/stable/creating/).

Run the deterministic tests without Node, Gmail credentials or network access:

```bash
venv_app/bin/python -m unittest tests.test_mcp_gmail_schema -v --buffer
```

The tests cover all 15 definitions, unchanged schemas/fingerprints, nested valid and
invalid queries, date patterns, numeric/enum/additional-property constraints, separate
enablement/authority/approval checks, frozen catalogs, drift/review, adversarial depth
and branching, concurrent counters, unsafe reference/regex rejection and redacted
diagnostics. Browser tests verify that the diagnostic is rendered as text. The manual
preview used real captured schemas plus a deliberately invalid variant; it did not
connect to the live Gmail setup or demonstrate mailbox operation success.
