#!/usr/bin/env python
"""Source-bound, machine-readable PRIVILEGED cross-identity credential PASS proof (E3).

Executes the REAL partition-credential separation proof against two actual service OS
identities with EXCLUSIVE groups and emits canonical JSON evidence (schema
``l2e-e3-credential-pass-v1``) bound to the exact Git source it was produced from.

It MUST run as root (it forks + setuid to each service identity to prove real filesystem
denial) and REFUSES to emit a PASS unless the Git worktree is clean, so the evidence can
be bound to an exact source commit/tree. CI keeps the unprivileged ``HOLD`` unit test;
E3 acceptance additionally requires one recorded PASS produced by this script on a
privileged qualification host, then committed as evidence E.

What it records (all measured, none inferred):
  * git_head, git_tree, worktree_clean, script_sha256, generated_at_utc
  * trainer/evaluator usernames, uids, and group memberships (gids)
  * train/validation root modes + gids, artifact modes + gids
  * the FOUR separately-measured cross-identity outcomes:
      trainer_train, trainer_validation, evaluator_validation, evaluator_train
  * the full verify_operational_credentials check map + overall status
  * evidence_hash = sha256 over the canonical JSON with ``evidence_hash`` excluded

Exit code 0 iff overall status is PASS AND all four measured outcomes are correct
(trainer_train=OK, trainer_validation=DENIED, evaluator_validation=OK,
evaluator_train=DENIED). Contains no passwords, secrets, or credentials — only OS
identity names, uids, gids, and mode bits.

Runbook (privileged qualification host; identities/groups are deployment-owned):

    sudo groupadd minos_train_grp && sudo groupadd minos_validation_grp
    sudo useradd -M -N -g minos_train_grp minos_trainer_svc
    sudo useradd -M -N -g minos_validation_grp minos_evaluator_svc
    sudo -E /usr/bin/python3 scripts/l2e_credential_pass_proof.py \
        --root /var/lib/minos/l2e_cred_proof \
        --trainer minos_trainer_svc --trainer-group minos_train_grp \
        --evaluator minos_evaluator_svc --evaluator-group minos_validation_grp \
        --output /tmp/L2E_E3_CREDENTIAL_PASS.json
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

EVIDENCE_SCHEMA = "l2e-e3-credential-pass-v1"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _worktree_clean() -> bool:
    return _git("status", "--porcelain") == ""


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _provision(root: Path, train_gid: int, validation_gid: int) -> None:
    """Traversable parents (0o755, no matrix files); 0o2750 partition roots; 0o640
    artifacts group-owned by the partition group."""
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    l2e = root / "l2e"
    l2e.mkdir(exist_ok=True)
    os.chmod(l2e, 0o755)
    for partition, gid in (("train", train_gid), ("validation", validation_gid)):
        part = l2e / partition
        part.mkdir(exist_ok=True)
        os.chown(part, os.getuid(), gid)
        os.chmod(part, 0o2750)
        artifact = part / f"{partition}-sample.parquet"
        artifact.write_bytes(f"canonical-{partition}-artifact-bytes".encode())
        os.chown(artifact, os.getuid(), gid)
        os.chmod(artifact, 0o640)


def _identity(username: str) -> dict[str, object]:
    entry = pwd.getpwnam(username)
    return {
        "username": username,
        "uid": entry.pw_uid,
        "group_gids": sorted(os.getgrouplist(username, entry.pw_gid)),
    }


def _mode_gid(path: Path) -> dict[str, object]:
    st = path.stat()
    return {"mode": oct(stat.S_IMODE(st.st_mode)), "gid": st.st_gid, "uid": st.st_uid}


def main() -> int:
    parser = argparse.ArgumentParser(description="L2-E E3 privileged credential PASS proof")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--trainer", required=True)
    parser.add_argument("--trainer-group", required=True)
    parser.add_argument("--evaluator", required=True)
    parser.add_argument("--evaluator-group", required=True)
    parser.add_argument("--output", type=Path, help="write the canonical JSON evidence here")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("STATUS: REFUSED — must run as root (real setuid impersonation).")
        return 2
    if not _worktree_clean():
        print("STATUS: REFUSED — Git worktree is not clean; cannot bind evidence to source.")
        return 2

    from minos_engine.storage.matrix_access import (
        _first_artifact,
        _open_exact_path_as,
        verify_operational_credentials,
    )

    train_gid = grp.getgrnam(args.trainer_group).gr_gid
    validation_gid = grp.getgrnam(args.evaluator_group).gr_gid
    _provision(args.root, train_gid, validation_gid)
    train_root = args.root / "l2e" / "train"
    validation_root = args.root / "l2e" / "validation"
    train_artifact = _first_artifact(train_root)
    validation_artifact = _first_artifact(validation_root)
    assert train_artifact is not None and validation_artifact is not None

    def _measure(username: str, path: Path) -> str:
        # privileged parent resolved the exact path; child drops to identity and opens it.
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(r)
            out = "ERR"
            try:
                out = _open_exact_path_as(username, path)
            finally:
                os.write(w, out.encode())
                os._exit(0)
        os.close(w)
        result = os.read(r, 16).decode()
        os.close(r)
        os.waitpid(pid, 0)
        return result

    outcomes = {
        "trainer_train": _measure(args.trainer, train_artifact),
        "trainer_validation": _measure(args.trainer, validation_artifact),
        "evaluator_validation": _measure(args.evaluator, validation_artifact),
        "evaluator_train": _measure(args.evaluator, train_artifact),
    }
    expected = {
        "trainer_train": "OK",
        "trainer_validation": "DENIED",
        "evaluator_validation": "OK",
        "evaluator_train": "DENIED",
    }
    outcomes_pass = outcomes == expected

    status = verify_operational_credentials(
        train_root=train_root,
        validation_root=validation_root,
        trainer_identity=args.trainer,
        evaluator_identity=args.evaluator,
    )
    overall_pass = status.status == "PASS" and outcomes_pass

    evidence: dict[str, object] = {
        "schema": EVIDENCE_SCHEMA,
        "git_head": _git("rev-parse", "HEAD"),
        "git_tree": _git("rev-parse", "HEAD^{tree}"),
        "worktree_clean": True,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "trainer": _identity(args.trainer),
        "evaluator": _identity(args.evaluator),
        "trainer_group": {"name": args.trainer_group, "gid": train_gid},
        "evaluator_group": {"name": args.evaluator_group, "gid": validation_gid},
        "train_root": _mode_gid(train_root),
        "validation_root": _mode_gid(validation_root),
        "train_artifact": _mode_gid(train_artifact),
        "validation_artifact": _mode_gid(validation_artifact),
        "measured_outcomes": outcomes,
        "expected_outcomes": expected,
        "outcomes_pass": outcomes_pass,
        "cross_identity_access_proven": bool(status.checks.get("cross_identity_access_proven")),
        "checks": dict(sorted(status.checks.items())),
        "reasons": list(status.reasons),
        "status": "PASS" if overall_pass else status.status,
    }
    evidence["evidence_hash"] = hashlib.sha256(
        _canonical({k: v for k, v in evidence.items() if k != "evidence_hash"}).encode()
    ).hexdigest()

    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(f"STATUS: {evidence['status']}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
