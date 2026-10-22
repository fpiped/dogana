# dogana working rules

Read the fpiped vault scope and the global rules before work. One Git worktree per work stream.

`docs/protocol.md` is canonical for every term and transition; code and README follow it, never the
reverse. A change in semantics lands in the protocol first, with its reason.

The core (`src/dogana`) imports only the standard library and never knows an extractor, a store or
a scheduler. Anything that performs I/O is a reference implementation in its own package. The test that the boundary
holds: with the reference package deleted, the ledger still validates a manifest, applies it and answers claims.

Every transition rule in the protocol has a test naming the delivery pattern it covers. A delivery may
never raise the ledger above what it proves; a test that passes by assuming otherwise is wrong.

No mention of tools used to write the code in any artefact. Commit messages state the decision, not
the diff.
