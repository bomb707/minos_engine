# Layer 2 — Entry Gate (L1-READY)

The entry gate proves that the accepted Layer 1 qualification exists, is unmodified,
and that the current repository genuinely descends from the accepted history. Layer
2 may not proceed otherwise. It fails closed on any missing git object, shallow or
incomplete history, and any divergent/sibling/rewritten/unrelated history.

## Repository-owned identities (no caller inputs)
All expected identities are pinned in `layer2/prerequisites.py` (`ACCEPTED`). The
public request carries **only** runtime locators:

```python
EntryGateRequest(repo_root=..., l1_ready_path=None, qualification_report_path=None, head_ref="HEAD")
```

Callers cannot select the expected gate hash, source commit/tree, schema hash,
profiler hash, or version (extra fields are forbidden, so an override attempt is
rejected at construction). Updating an accepted identity is an owner decision made by
editing `prerequisites.py` — never an environment, CLI, or caller input.

## The 34 proven invariants
1. `l1-ready.json` exists. 2. It parses. 3. `gate_name == "L1-READY"`. 4. `status ==
PASS`. 5. Canonical `gate_hash` is internally valid. 6. Gate hash equals the pinned
accepted L1 gate hash. 7. All required L1 checks present. 8. Every mandatory check
true. 9. Every evidence entry exists and re-hashes (git-bound). 10. Qualification
report SHA-256 matches the gate. 11–13. Layer 1 schema hash / profiler config hash /
profiler version equal the pinned values. 14–15. Gate-recorded qualified source
commit/tree equal the accepted. 16. Qualified source commit exists. 17. Its tree
equals the accepted tree. 18. Accepted artifact commit exists. 19. Its tree equals
the accepted artifact tree. 20. Artifact **properly** descends the source. 21. HEAD
descends the artifact. 22. v2 framework commit exists and descends the artifact.
23. v2 evidence descends the framework. 24. Owner acceptance commit descends v2
evidence. 25. HEAD descends the owner commit. 26. All pinned git objects present.
27. History not shallow/incomplete. 28. No divergent/sibling/unrelated history.
29. Evidence non-empty. 30. Evidence paths unique. 31. No evidence path escapes the
repo (absolute / `..`). 32. No symlink-based evidence escape. 33. Gate fields well
formed. 34. Reason codes are deterministic and machine-readable.

## Reason codes
Failures return stable, machine-readable codes (e.g. `L1_READY_MISSING`,
`GATE_HASH_NOT_ACCEPTED`, `EVIDENCE_HASH_MISMATCH`, `ARTIFACT_NOT_DESCENDANT_OF_SOURCE`,
`HEAD_NOT_DESCENDANT_OF_OWNER`, `GIT_HISTORY_SHALLOW`, `NOT_A_GIT_REPO`,
`EVIDENCE_PATH_ESCAPE`, `EVIDENCE_SYMLINK_ESCAPE`). Git ancestry is proven with
`git merge-base --is-ancestor` — never by parent equality, commit messages,
timestamps, branch names, caller booleans, or filenames.

## Not an unblock
A passing entry gate proves the L2-A prerequisite. It does **not** unblock
`Layer2Service.select_config`, which continues to raise `StageNotReadyError`.
