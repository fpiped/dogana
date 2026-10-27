# dogana working rules

Working rules for this repository. One Git worktree per work stream; the shared checkout stays clean.

`docs/protocol.md` is canonical for every term and transition; code and README follow it, never the
reverse. A change in semantics lands in the protocol first, with its reason.

The core (`src/dogana`) imports only the standard library and never knows an extractor, a store or
a scheduler. The package never parses a data file: a store, a file drop or anything that reads rows is an
example under `examples/`, outside the package. The test that the boundary holds: a copy of the core's
modules alone still validates a manifest, applies it and answers claims.

Every transition rule in the protocol has a test naming the delivery pattern it covers. A delivery may
never raise the ledger above what it proves; a test that passes by assuming otherwise is wrong.

No mention of tools used to write the code in any artefact. Commit messages state the decision, not
the diff.
