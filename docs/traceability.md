# Protocol traceability

Run `python scripts/traceability.py` in the development environment. It collects the real pytest
suite without running tests.
`--list` prints every ID, its status and its source line. `--strict` also fails on coverage gaps.

CI runs with `--strict` and fails on unclaimed rules, unknown claims, malformed markers, stale
exemptions, duplicate protocol IDs, unlabelled prose or failed test collection. The five coverage
gaps are closed. A marker records relevant assertions, not exhaustive clause coverage or proof of
a source's promises. Existing tests retain their delivery-pattern names.

## IDs

An ID names a normative unit: a numbered item, named prose block, definition bullet or table row.
Sentences within one unit share its ID. Names come from the document, not a separate registry:

- A section's lowercase, hyphenated heading is its prefix. Invisible `protocol-section` comments
  shorten four prefixes to `contract`, `checks`, `claims` and `receiver`, preserving site anchors.
- A bold sub-part adds its lowercase, hyphenated name, such as `contract.types`.
- A numbered item adds its literal Markdown number, such as `transitions.3` or
  `contract.shapes.2`. Numbers belong to rules: never renumber existing items. Insert a rule with
  an unused number; Markdown still renders the list in order. No list index is computed.
- `protocol-scope` comments name lists introduced by prose, such as `receiver.obligations.2`.
  A plain one-line caption ending in a colon introduces its list without adding a rule.
- A table row adds its first cell's slug, preserving underscores, such as `checks.row_count`.
  Transition rows have short invisible labels, such as `transitions.timestamp-tombstones-covered`,
  so editing their explanatory text cannot rename them or collapse opposite inequalities.
- `<!-- protocol: name -->` labels a prose paragraph or definition bullet with no natural name.
  Before a bold sub-part it can also preserve that sub-part's ID across a heading edit.
  A label is local to its section; its text is not an ordinal counter.

HTML comments leave the rendered prose and anchors unchanged. Keep labels when moving or editing
their rule. New prose must be labelled, so it cannot silently escape the inventory. Duplicate IDs
fail instead of overwriting a rule. Numbered lists stay under their last bold sub-part or explicit
scope until the next one; a section heading resets both.

Claim only behavior a test actually asserts, using
`@pytest.mark.protocol("transitions.3", "manifest.4")`. Pytest registers the marker in
`pyproject.toml`; the collection plugin accepts one or more string IDs and no keyword arguments.
Premises, definitions and explicitly undecided statements that have no receiver behavior are
exempted with reasons in `UNTESTABLE` in `scripts/traceability.py`, the only exemption list.
Mixed units still need tests for their observable clauses. Claiming an exempt unit fails until
the claim or exemption is corrected.

## Coverage

Every unit is accounted for: claimed by at least one test, or exempted with a reason in
`UNTESTABLE`. Exempt units are definitions, premises about the source, the query theorem, and what
only the receiver's database can show: a reader's isolation and the persistence of a group's
receipts. Run `.venv/bin/python scripts/traceability.py` for the current counts.

The original mapping audit verified stable IDs after insertions, failure on unknown inherited
claims, malformed markers, duplicate IDs, unlabelled prose and stale exemptions, and failure on
gaps with `--strict`. Markdown rendering kept the protocol's visible text, table content, links
and heading anchors unchanged.
