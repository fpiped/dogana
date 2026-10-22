# The closedworld protocol

Version 0, design stage. Terms defined here are used unchanged in code, receipts and documentation.

## The problem it names

A table in a landing zone is the fold of its deliveries. Each delivery was produced by some read of the
source, and the read decides what the receiver may conclude from the rows that are absent. A full
extract proves a missing row is deleted. A file of rows changed since Monday proves nothing about
deletions. A window of the last thirty days proves deletions inside the window and nothing outside it.

The database literature calls the first case the closed world assumption: what is not in the table is
false. Razniewski and Nutt formalise partial knowledge as table completeness statements, "the available
table holds every ideal row satisfying condition G", and Motro splits integrity into completeness (no
missing rows) and validity (no rows that should not be there). A row deleted at the source but still
present at the receiver is a validity failure, not a completeness one. The ledger therefore carries both
halves, per region of each table.

## Delivery contract

One per table, declared once by the receiver (in pull mode it also parameterises the read). It binds the
table's identity to the shapes its deliveries may take:

- `table`: the landing table.
- `key`: the columns rows are matched on. Empty means append-only.
- `window_column`: the column window shapes are bounded on, present exactly when a window shape exists.
- `shapes`: named shapes, at least one. A table fed by a daily window and a weekly full export declares
  two, and each manifest names the one it is.

A shape is four properties, each with a closed set of values:

| Property | Values | Meaning |
|---|---|---|
| coverage | `all`, `window`, `keys` | The part of the key space the delivery speaks for. `window` is bounded on the window column; a partition is a window whose low and high coincide. `keys` means the delivery names the keys it carries and speaks for nothing else. |
| completeness | `complete`, `changed_only` | Whether every current source row inside the coverage is present. Inside a complete delivery, absence means deletion. |
| deletions | `absence`, `tombstone`, `none` | How a deletion reaches the receiver. |
| checkpoint | `generation`, `timestamp`, `none` | What "since" is anchored to for a changed-only delivery. A generation is a token the receiver holds from the previous delivery; a timestamp is a source column. |

Validity rules, enforced at construction:

- `complete` if and only if `checkpoint` is `none`: a complete delivery has no "since", a changed-only
  delivery always has one.
- `complete` implies `deletions = absence`; `changed_only` implies `deletions` is `tombstone` or `none`.
- `coverage = keys` implies `changed_only`.
- A contract with an empty key admits only shapes that are `changed_only` with `deletions = none`:
  without identity there is nothing to delete or update, only to append.

These rules leave fourteen valid shapes. The common source patterns map onto them:

| Source pattern | Shapes declared |
|---|---|
| Nightly full export | `full`: all, complete, absence, none |
| Daily 30-day window, weekly full | `daily`: window, complete, absence, none; `weekly`: all, complete, absence, none |
| Rows changed since last run, no delete signal, weekly full | `since`: all, changed_only, none, timestamp; `weekly` as above |
| Change feed with tombstones from the generation we hold | `feed`: keys, changed_only, tombstone, generation; `full` as a safety net |
| Export per plant | `plant`: window on the plant column, complete, absence, none |
| Append-only event log | `log`: all, changed_only, none, timestamp |

## Manifest

One per delivery. It states what the delivery claims; the receiver verifies the claim before applying
the rows. Written by the sender as a sidecar, derived by the receiver from a declared file pattern, or
produced by the receiver's own extraction.

Fields: `table`, `shape` (the name of the shape it claims), `produced_at` (the moment the source was read), `window` (low and high, required for
window coverage), `since` (the generation token or timestamp the delivery starts from, required for
changed-only deliveries), `generation` (the token the source assigned to this delivery, when the source
issues them), `rows`, `schema_hash`.

A manifest that contradicts its shape (a shape the contract does not admit, a window for an `all` shape, a
missing `since` for a changed-only one) is rejected before any row is read. A manifest whose rows contradict it (rows outside
the declared window, a row count that does not match) is rejected by the adapter that reads the rows;
the core defines the checks, the adapter runs them.

## Ledger

Per table, a set of disjoint regions over the window column (one unbounded region when the contract has
no window column), each carrying:

- `complete_as_of`: the latest `produced_at` that proved no row was missing in the region.
- `valid_as_of`: the latest `produced_at` that proved no deleted row remained in the region.

and, per table, the `generation` held, when the source issues tokens.

Derived state per region, against a freshness limit:

| State | Condition | Reading |
|---|---|---|
| unknown | no delivery covered the region | nothing may be concluded |
| complete | `complete_as_of = valid_as_of`, within the limit | closed world holds as of that moment |
| patched | `complete_as_of > valid_as_of`, within the limit | present rows are current, rows deleted at the source after `valid_as_of` may remain |
| expired | `complete_as_of` older than the limit | the region is stale regardless of its shape |

## Transitions

One rule governs them all: a delivery never raises a date above what it proves.

- Complete delivery over coverage C at time t: every region inside C becomes (t, t). Regions crossing
  the boundary of C are split; regions outside C are untouched.
- Changed-only delivery with a generation checkpoint: `since` must equal the generation held, else the
  delivery is rejected. A feed is meaningful only against the state it was computed from; after a
  rejection the receiver asks for a complete delivery.
- Changed-only delivery before any complete delivery of the table: rejected, there is no base to patch.
- Changed-only delivery at time t over regions R (all regions for `all` and `keys` coverage, the regions
  crossing the window for `window` coverage): `complete_as_of` becomes t in R. `valid_as_of` becomes t
  only when `deletions = tombstone`; with `deletions = none` it does not move, and the regions become
  patched. Only a complete delivery moves it again.
- A manifest carrying a `generation` replaces the generation held.
- Time: a region whose `complete_as_of` is older than the limit is expired.

## Claims

Readers never see the ledger directly. They see claims derived from it for a table or for the set of
tables a surface reads: the oldest `complete_as_of` and `valid_as_of` across the regions involved, whether
any region is unknown, patched or expired, and the regions in each state. A figure that depends on
absence (items with no movement, suppliers with no invoices) is trustworthy only over complete regions.

## Receipt

One per delivery, complete or rejected: the manifest as received, the checks run with their outcomes,
the transition applied, the resulting claims, and the identity of the publication that made the rows
visible. Receipts are the audit trail; the ledger is derivable from them.

## Entry points

Push: the sender delivers rows and a manifest, or rows alone with the manifest derived from the pattern
the contract declares. The manifest is a claim to be verified.

Pull: the receiver's extractor takes the contract as input. Coverage and checkpoint parameterise the
read and the manifest is written by the code that ran it, so the two cannot disagree. With pull, the
ledger also runs backwards: from the claim a reader needs (deletions known as of today) to the cheapest
delivery that reaches it (a complete delivery of that region).

## Boundaries

The protocol covers declaring, verifying, accounting and claiming. It does not extract (the extractor
does), does not schedule (the orchestrator does), does not evaluate row-level rules (the quality tool
does) and does not store rows (the table format does). Adapters translate between the protocol and
those tools; the core must stay correct with all of them removed.
