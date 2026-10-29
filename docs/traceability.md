# Protocol traceability

Run `python scripts/traceability.py` in the development environment. It collects the real pytest
suite without running tests, including the inherited tests from `src/dogana/conformance`.
`--list` prints every ID, its status and its source line. `--strict` also fails on coverage gaps.

CI fails on unknown claims, malformed markers, stale exemptions, duplicate protocol IDs,
unlabelled prose or failed test collection. Unclaimed rules are reported without failing for now:
the existing gaps must be closed before CI can enforce complete coverage. Enable `--strict` in
the workflow when they are closed. A marker records relevant assertions, not exhaustive clause
coverage or proof of a source's promises. Existing tests retain their delivery-pattern names.

## IDs

An ID names a normative unit: a numbered item, named prose block, definition bullet or table row.
Sentences within one unit share its ID. Names come from the document, not a separate registry:

- A section's lowercase, hyphenated heading is its prefix. Invisible `protocol-section` comments
  shorten four prefixes to `contract`, `checks`, `claims` and `store`, preserving site anchors.
- A bold sub-part adds its lowercase, hyphenated name, such as `contract.types`.
- A numbered item adds its literal Markdown number, such as `transitions.3` or
  `contract.shapes.2`. Numbers belong to rules: never renumber existing items. Insert a rule with
  an unused number; Markdown still renders the list in order. No list index is computed.
- `protocol-scope` comments name lists introduced by prose, such as `store.obligations.2`.
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
Conformance markers live on the base methods, so every collected implementation inherits them.
Premises, definitions and explicitly undecided statements that have no receiver behavior are
exempted with reasons in `UNTESTABLE` in `scripts/traceability.py`, the only exemption list.
Mixed units still need tests for their observable clauses. Claiming an exempt unit fails until
the claim or exemption is corrected.

## Current gaps

The audit of `715ea82` with the added markers finds 135 units: 118 claimed, 5 unclaimed and
12 marked untestable. All numbered transition rules and all eleven transition table rows are
claimed by the mapping of 296 test functions. No new behavioral tests were added.

- `checks.window_present`: Deliver a full file with a null window value and assert a
  `window_present` refusal without changing the publication.
- `claims.3`: Compare an anti-join and exact aggregates against source truth after an unreported
  deletion, showing the reversed requirements and the need for both dates.
- `contract.deleted-content`: Deliver two hashless tombstone files with equal manifests but
  different deleted keys and assert the second replays without reading it, then contrast hashes.
- `contract.soft-delete-fallback`: Model a source that physically removes a row after flagging it
  and show that a no-deletions changes shape retains it until a complete read removes it.
- `trust-model.1`: Deliver a truncated full export with a matching receiver count and assert
  acceptance, then refuse it with the larger count declared by the source's original read.

The review found no pre-existing protocol markers. During mapping,
`test_schema_failure_marks_dependent_checks_not_evaluated` was left claiming only `checks.blocked`:
its `window_present` answer is deliberately not evaluated, so it does not test that predicate.
`test_a_failed_required_check_never_changes_the_published_ledger` exercises the gate's rejected
receipt, not the store's persistence step. Neither test claims those additional rules.

Some claims cover only part of a longer unit. For example, the held-file mutation test exercises
the stable copy during checks and aborts before publication; correction tests cover a new hash and
later boundary, not rebuilding rows from retained receipts. Numeric-cost and constructor tests
cover cost representation and the default, and planner tests cover selection; no explicit negative
`Shape.cost` scenario was found. These limits remain visible in review even though the units have
relevant test claims. Snapshot truth, the theorem's premises and the source's soft-delete promises
remain outside what receiver tests can establish.

Validation: 1000 tests passed and 14 skipped on Python 3.14.7; Ruff, strict mypy (including the
script) and the strict site build passed. Manual checks verified stable IDs after insertions,
failure on unknown inherited claims, malformed markers, duplicate IDs, unlabelled prose and
stale exemptions, and failure on gaps with `--strict`. Markdown rendering kept the protocol's
visible text, table content, links and heading anchors unchanged.
