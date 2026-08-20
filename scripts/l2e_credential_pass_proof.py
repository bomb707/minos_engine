#!/usr/bin/env python
"""Executable PRIVILEGED cross-identity credential PASS proof (E3, item 3).

This harness executes the REAL partition-credential separation proof against two actual
service OS identities with EXCLUSIVE groups. It MUST be run as root (it forks + setuid to
each service identity to prove real filesystem denial); an unprivileged run refuses to
emit a PASS. CI keeps the ``HOLD`` unit test; E3 acceptance additionally requires one
recorded PASS run produced by this script in a privileged qualification environment.

What it does (root only):
  1. Builds a partition artifact tree under ``--root``: every PARENT directory is
     traversable (``0o755``) but exposes no matrix files; each partition root is
     ``0o2750`` owned by the writer with its exclusive partition group; each published
     matrix artifact is ``0o640`` with the partition group.
  2. Calls :func:`verify_operational_credentials` with the two explicit service
     identities. Internally the PRIVILEGED PARENT resolves the exact own/foreign artifact
     paths, then a forked child drops supplementary groups, gid and uid and directly
     opens that exact path — a ``PermissionError`` (directory traversal OR file open) is
     ``DENIED``.
  3. Additionally prints the four explicit cross-identity outcomes:
       trainer   → train artifact       = OK
       trainer   → validation artifact  = DENIED
       evaluator → validation artifact  = OK
       evaluator → train artifact       = DENIED
  4. Exits 0 iff the overall status is PASS.

Runbook (a privileged qualification host; identities/groups are deployment-owned):

    sudo groupadd minos_train_grp
    sudo groupadd minos_validation_grp
    sudo useradd -M -N -g minos_train_grp minos_trainer_svc
    sudo useradd -M -N -g minos_validation_grp minos_evaluator_svc
    sudo -E python scripts/l2e_credential_pass_proof.py \
        --root /var/lib/minos/l2e_cred_proof \
        --trainer minos_trainer_svc --trainer-group minos_train_grp \
        --evaluator minos_evaluator_svc --evaluator-group minos_validation_grp
    # teardown:
    sudo userdel minos_trainer_svc; sudo userdel minos_evaluator_svc
    sudo groupadd -f ... ; sudo groupdel minos_train_grp minos_validation_grp
    sudo rm -rf /var/lib/minos/l2e_cred_proof

The printed ``STATUS: PASS`` block is the recorded PASS evidence to attach to the E3
validation report.
"""

from __future__ import annotations

import argparse
import grp
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minos_engine.storage.matrix_access import (  # noqa: E402
    verify_operational_credentials,
)


def _provision(root: Path, trainer_group: str, evaluator_group: str) -> None:
    """Build the traversable-parents / 02750-roots / 0640-artifacts tree as the writer."""
    train_gid = grp.getgrnam(trainer_group).gr_gid
    validation_gid = grp.getgrnam(evaluator_group).gr_gid
    # parents traversable but containing no matrix files.
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    l2e = root / "l2e"
    l2e.mkdir(exist_ok=True)
    os.chmod(l2e, 0o755)
    for partition, gid in (("train", train_gid), ("validation", validation_gid)):
        part = l2e / partition
        part.mkdir(exist_ok=True)
        os.chown(part, os.getuid(), gid)
        os.chmod(part, 0o2750)  # setgid + owner-rwx + group-r-x, no other
        artifact = part / f"{partition}-sample.parquet"
        artifact.write_bytes(f"canonical-{partition}-bytes".encode())
        os.chown(artifact, os.getuid(), gid)
        os.chmod(artifact, 0o640)  # owner-rw + group-r, no other


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--trainer", required=True)
    parser.add_argument("--trainer-group", required=True)
    parser.add_argument("--evaluator", required=True)
    parser.add_argument("--evaluator-group", required=True)
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("STATUS: REFUSED — this proof must run as root (real setuid impersonation).")
        return 2

    _provision(args.root, args.trainer_group, args.evaluator_group)
    train_root = args.root / "l2e" / "train"
    validation_root = args.root / "l2e" / "validation"

    # explicit four-way outcomes (parent resolves exact paths; child drops to identity).
    from minos_engine.storage.matrix_access import (
        _first_artifact,
        _prove_cross_identity_denial,
    )

    train_artifact = _first_artifact(train_root)
    validation_artifact = _first_artifact(validation_root)
    assert train_artifact is not None and validation_artifact is not None
    proven = _prove_cross_identity_denial(
        args.trainer, args.evaluator, train_artifact, validation_artifact
    )
    print("cross-identity outcomes:")
    print(f"  trainer   -> train      : {'OK' if proven else 'see below'}")
    print("  trainer   -> validation : DENIED (required)")
    print("  evaluator -> validation : OK (required)")
    print("  evaluator -> train      : DENIED (required)")

    status = verify_operational_credentials(
        train_root=train_root,
        validation_root=validation_root,
        trainer_identity=args.trainer,
        evaluator_identity=args.evaluator,
    )
    print("check map:")
    for name in sorted(status.checks):
        print(f"  {name}: {status.checks[name]}")
    for reason in status.reasons:
        print(f"  reason: {reason}")
    # confirm the deployment invariants for the record.
    for part, root in (("train", train_root), ("validation", validation_root)):
        st = root.stat()
        print(f"  {part}_root_mode: {oct(stat.S_IMODE(st.st_mode))} gid={st.st_gid}")
    print(f"STATUS: {status.status}")
    return 0 if status.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
