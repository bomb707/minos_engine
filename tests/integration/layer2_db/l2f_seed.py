"""Reusable deterministic seeder for a complete, internally valid L2-F graph (F3-A4).

After a scratch database is upgraded to ``0006_l2f_experiment_plan`` this module inserts a
full upstream (L2-D/L2-E) + L2-F graph through direct SQL, using deterministic UUIDs and
hashes so every row — and therefore every attack-matrix failure — is reproducible.

The seed is split into two phases so the populated migration-lifecycle test can prove that a
``0006`` downgrade preserves the upstream rows and a subsequent re-upgrade accepts fresh
L2-F rows against them:

* ``seed_upstream_graph`` inserts only the L2-D/L2-E tables (0004/0005 owned), returning
  ``UpstreamRefs``.
* ``seed_l2f_graph`` inserts only the five ``0006`` L2-F tables against those refs.
* ``seed_valid_graph`` composes both and returns the full ``SeededGraph``.

Design notes (all DB-valid; semantic completeness is F3-B/F3-D, not F3-A):

* Two profile snapshots ``SA`` (epoch 1) and ``SB`` (epoch 2); ``SA`` carries train,
  validation and test snapshot members.
* Two train feature matrices ``MA`` (SA/train) and ``MB`` (SB/train), plus one validation
  matrix ``MV`` (SA/validation) used only as a non-train probe target.
* A handful of datasets are deliberately cross-wired so single-constraint attack isolation
  is possible: ``D8`` is an ``SB`` snapshot member *and* an ``MA`` matrix member (probes
  "snapshot member from another snapshot"); ``D9`` is an ``SA`` snapshot member *and* an
  ``MB`` matrix member (probes "matrix member from another matrix"); ``D2``/``D3`` are
  validation/test snapshot members that are also ``MA`` matrix members (so the matrix-member
  FK is satisfiable while the snapshot-member FK rejects the wrong partition).
* ``D7`` is a valid ``SA`` train snapshot+matrix member that is **not** enrolled as a Plan A
  member — the reusable "probe slot" for member lineage-forgery attacks.
* Plan A enrolls members {D1, D4} (train_member_count=2) over configs {CP1, CP2}
  (candidate_count=2), logical_job_count=4. Only three of the four planned jobs are
  materialized; the (D4, CP2) combo is reserved as a valid unseeded slot for the
  duplicate-job_key / invalid-status / positive-control probes. F3-A does not enforce that
  all planned jobs are materialized (that is F3-C enqueue).
* ``CP4`` is a valid Plan-A parameter-space payload that is intentionally **not** linked as a
  plan-config, reserved for the duplicate-config_index probe.

The connection seeds inside the caller's open transaction (rows are visible to later
statements) and runs as ``minos_admin`` so it owns every insert.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

CONFIG_MEDIA_TYPE = "application/vnd.minos.l2f-config+json"
CONFIG_SCHEMA_VERSION = "l2f-config-payload-v1"
_OTHER_MEDIA_TYPE = "application/octet-stream"

_NS = uuid.UUID("0000000f-2f2f-2f2f-2f2f-00000000f2f2")


def U(label: str) -> str:
    """Deterministic UUID for a stable label."""
    return str(uuid.uuid5(_NS, label))


def H(label: str) -> str:
    """Deterministic 64-hex hash for a stable label."""
    return hashlib.sha256(label.encode()).hexdigest()


def _insert(
    conn: Connection,
    schema: str,
    table: str,
    row: dict[str, Any],
    jsonb_cols: tuple[str, ...] = (),
) -> None:
    cols = list(row)
    vals = [f"CAST(:{c} AS jsonb)" if c in jsonb_cols else f":{c}" for c in cols]
    sql = f"INSERT INTO {schema}.{table} ({', '.join(cols)}) VALUES ({', '.join(vals)})"  # noqa: S608
    conn.execute(text(sql), row)


DATASETS = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9")
_CHROMS = ("chr18", "chr19", "chr20", "chr21", "chr22")


@dataclass(frozen=True)
class UpstreamRefs:
    """Identifiers of the seeded L2-D/L2-E rows the L2-F graph references."""

    sa: str
    sb: str
    sa_snap: str
    sa_split: str
    sa_reg: str
    sb_snap: str
    sb_split: str
    sb_reg: str
    fsa: str
    fsa_hash: str
    fsa_reg: str
    fsb: str
    fsb_hash: str
    fsb_reg: str
    ma: str
    ma_hash: str
    mb: str
    mb_hash: str
    mv: str
    mv_hash: str
    dsr: dict[str, str]
    bam: dict[str, str]
    fvh: dict[str, str]
    psm: dict[tuple[str, str], str]
    fmm: dict[tuple[str, str], str]
    fmm_index: dict[tuple[str, str], int]
    agen: str
    ar: dict[str, str]
    ch: dict[str, str]
    ps_a: str
    ps_b: str
    grh: str
    epp: str


@dataclass(frozen=True)
class SeededGraph:
    """Full graph identifiers + valid-baseline row builders for the attack matrix."""

    up: UpstreamRefs
    cp1: str
    cp2: str
    cp3: str
    cp4: str
    pa: str
    pa_hash: str
    pb: str
    pb_hash: str
    pm_d1: str
    pm_d4: str
    pm_d5: str
    pc1: str
    pc2: str
    pcb: str
    j1: str
    j2: str
    j3: str
    jb: str
    jk1: str
    jk2: str
    jk3: str
    jkb: str

    # convenience passthroughs to upstream refs (keeps attack builders terse)
    @property
    def sa(self) -> str:
        return self.up.sa

    @property
    def sb(self) -> str:
        return self.up.sb

    @property
    def sa_snap(self) -> str:
        return self.up.sa_snap

    @property
    def sa_split(self) -> str:
        return self.up.sa_split

    @property
    def sa_reg(self) -> str:
        return self.up.sa_reg

    @property
    def sb_snap(self) -> str:
        return self.up.sb_snap

    @property
    def sb_split(self) -> str:
        return self.up.sb_split

    @property
    def sb_reg(self) -> str:
        return self.up.sb_reg

    @property
    def fsa(self) -> str:
        return self.up.fsa

    @property
    def fsa_hash(self) -> str:
        return self.up.fsa_hash

    @property
    def fsa_reg(self) -> str:
        return self.up.fsa_reg

    @property
    def fsb(self) -> str:
        return self.up.fsb

    @property
    def fsb_hash(self) -> str:
        return self.up.fsb_hash

    @property
    def fsb_reg(self) -> str:
        return self.up.fsb_reg

    @property
    def ma(self) -> str:
        return self.up.ma

    @property
    def ma_hash(self) -> str:
        return self.up.ma_hash

    @property
    def mb(self) -> str:
        return self.up.mb

    @property
    def mb_hash(self) -> str:
        return self.up.mb_hash

    @property
    def mv(self) -> str:
        return self.up.mv

    @property
    def mv_hash(self) -> str:
        return self.up.mv_hash

    @property
    def dsr(self) -> dict[str, str]:
        return self.up.dsr

    @property
    def bam(self) -> dict[str, str]:
        return self.up.bam

    @property
    def fvh(self) -> dict[str, str]:
        return self.up.fvh

    @property
    def psm(self) -> dict[tuple[str, str], str]:
        return self.up.psm

    @property
    def fmm(self) -> dict[tuple[str, str], str]:
        return self.up.fmm

    @property
    def fmm_index(self) -> dict[tuple[str, str], int]:
        return self.up.fmm_index

    @property
    def agen(self) -> str:
        return self.up.agen

    @property
    def ar(self) -> dict[str, str]:
        return self.up.ar

    @property
    def ch(self) -> dict[str, str]:
        return self.up.ch

    @property
    def ps_a(self) -> str:
        return self.up.ps_a

    @property
    def ps_b(self) -> str:
        return self.up.ps_b

    @property
    def grh(self) -> str:
        return self.up.grh

    @property
    def epp(self) -> str:
        return self.up.epp

    # ---- valid baseline row builders (insert-as-is => success) ----
    def new_plan(self, **over: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": U("attack_plan"),
            "profile_snapshot_id": self.sa,
            "train_feature_matrix_id": self.ma,
            "feature_set_id": self.fsa,
            "partition": "train",
            "snapshot_hash": self.sa_snap,
            "split_manifest_hash": self.sa_split,
            "registry_snapshot_hash": self.sa_reg,
            "train_matrix_hash": self.ma_hash,
            "train_feature_view_hash": H("attack_plan_tfv"),
            "feature_set_hash": self.fsa_hash,
            "feature_registry_hash": self.fsa_reg,
            "gatk_registry_hash": self.grh,
            "parameter_space_hash": H("attack_plan_ps"),
            "experiment_parameter_policy_hash": H("attack_plan_epp"),
            "candidate_set_hash": H("attack_plan_csh"),
            "train_member_count": 2,
            "candidate_count": 2,
            "logical_job_count": 4,
            "plan_hash": H("attack_plan_hash"),
        }
        row.update(over)
        return row

    def new_member(self, **over: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": U("attack_member"),
            "plan_id": self.pa,
            "profile_snapshot_id": self.sa,
            "feature_matrix_id": self.ma,
            "profile_snapshot_member_id": self.psm[("SA", "D7")],
            "feature_matrix_member_id": self.fmm[("MA", "D7")],
            "bam_profile_id": self.bam["D7"],
            "dataset_registry_id": self.dsr["D7"],
            "partition": "train",
            "feature_values_hash": self.fvh["D7"],
            "member_index": self.fmm_index[("MA", "D7")],
        }
        row.update(over)
        return row

    def new_config_payload(self, **over: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": U("attack_payload"),
            "config_hash": self.ch["SM"],
            "parameter_space_hash": self.ps_a,
            "schema_version": CONFIG_SCHEMA_VERSION,
            "media_type": CONFIG_MEDIA_TYPE,
            "artifact_id": self.ar["SM"],
        }
        row.update(over)
        return row

    def new_plan_config(self, **over: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": U("attack_plan_config"),
            "plan_id": self.pa,
            "config_payload_id": self.cp4,
            "config_hash": self.ch["CH4"],
            "parameter_space_hash": self.ps_a,
            "config_index": 2,
        }
        row.update(over)
        return row

    def new_job(self, **over: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": U("attack_job"),
            "plan_id": self.pa,
            "plan_member_id": self.pm_d4,
            "plan_config_id": self.pc2,
            "job_key": H("attack_job_key"),
            "status": "PENDING",
        }
        row.update(over)
        return row


def _dataset_row(label: str, idx: int) -> dict[str, Any]:
    return {
        "id": U(f"dsr:{label}"),
        "dataset_id": f"ds-{label}",
        "round_id": f"round-{label}",
        "chromosome": _CHROMS[idx % len(_CHROMS)],
        "region_source": "test",
        "region_start0": 0,
        "region_end0_exclusive": 1000,
        "region_length_bp": 1000,
        "region_coordinate_system": "zero-based-half-open",
        "region_hash": H(f"region:{label}"),
        "bam_sha256": H(f"bam:{label}"),
        "bai_sha256": H(f"bai:{label}"),
        "reference_sha256": H(f"ref:{label}"),
        "fai_sha256": H(f"fai:{label}"),
        "bam_size_bytes": 1024,
        "parameter_space_hash": H(f"dsr_ps:{label}"),
        "feature_registry_hash": H(f"dsr_fr:{label}"),
        "identity_tuple_hash": H(f"idtuple:{label}"),
        "manifest_hash": H(f"dsr_manifest:{label}"),
        "split_algorithm_version": "v1",
        "split_salt": "salt",
        "allocation_digest": H(f"alloc:{label}"),
    }


def _bam_row(label: str, dsr_id: str, agen: str) -> dict[str, Any]:
    return {
        "id": U(f"bam:{label}"),
        "dataset_registry_id": dsr_id,
        "profile_id": f"profile-{label}",
        "bam_sha256": H(f"bam:{label}"),
        "bai_sha256": H(f"bai:{label}"),
        "reference_sha256": H(f"ref:{label}"),
        "fai_sha256": H(f"fai:{label}"),
        "region_hash": H(f"region:{label}"),
        "identity_tuple_hash": H(f"bam_idtuple:{label}"),
        "m5_status": "MATCH",
        "integrity_degraded": False,
        "attestation_hash": H(f"attest:{label}"),
        "registry_snapshot_hash": H(f"bam_reg:{label}"),
        "profile_status": "COMPLETE",
        "profiler_version": "v1",
        "profiler_config_hash": H(f"profiler_cfg:{label}"),
        "windows_row_count": 10,
        "feature_values_hash": H(f"bam_fvh:{label}"),
        "l1_feature_values_hash": H(f"l1_fvh:{label}"),
        "eligible_value_count": 10,
        "profile_document": "{}",
        "profile_sha256": H(f"profile_sha:{label}"),
        "profile_manifest_sha256": H(f"profile_manifest:{label}"),
        "windows_sha256": H(f"windows_sha:{label}"),
        "profile_artifact_id": agen,
        "profile_manifest_artifact_id": agen,
        "windows_artifact_id": agen,
        "ingestion_key": H(f"ingestion:{label}"),
        "content_hash": H(f"content:{label}"),
    }


def seed_upstream_graph(conn: Connection) -> UpstreamRefs:
    """Insert only the L2-D/L2-E upstream rows; return their identifiers."""
    conn.execute(text("SET ROLE minos_admin"))

    ps_a, ps_b = H("param_space_A"), H("param_space_B")
    grh, epp = H("gatk_registry"), H("exp_policy")

    agen = U("artifact:GEN")
    _insert(
        conn,
        "catalog",
        "artifacts",
        {
            "id": agen,
            "uri": "mem://gen",
            "sha256": H("artifact_gen"),
            "media_type": _OTHER_MEDIA_TYPE,
        },
    )

    dsr = {d: U(f"dsr:{d}") for d in DATASETS}
    bam = {d: U(f"bam:{d}") for d in DATASETS}
    fvh = {d: H(f"fvh:{d}") for d in DATASETS}
    for idx, d in enumerate(DATASETS):
        _insert(conn, "catalog", "dataset_registry", _dataset_row(d, idx))
    for d in DATASETS:
        _insert(
            conn,
            "profiling",
            "bam_profiles",
            _bam_row(d, dsr[d], agen),
            jsonb_cols=("profile_document",),
        )

    ss1, ss2 = U("ss:1"), U("ss:2")
    _insert(
        conn,
        "catalog",
        "split_snapshots",
        {
            "id": ss1,
            "epoch": 1,
            "salt": "salt",
            "split_policy_version": "v1",
            "policy_hash": H("policy:1"),
            "manifest_hash": H("ss_manifest:1"),
            "registry_snapshot_hash": H("ss_reg:1"),
            "ancestor_v1_dataset_registry_hash": H("ancestor:1"),
            "parent_registry_snapshot_hash": None,
            "parent_manifest_hash": None,
            "parent_snapshot_id": None,
            "parent_epoch": None,
            "transition_count": 0,
            "sample_count": 3,
            "count_train": 1,
            "count_validation": 1,
            "count_test": 1,
        },
    )
    _insert(
        conn,
        "catalog",
        "split_snapshots",
        {
            "id": ss2,
            "epoch": 2,
            "salt": "salt",
            "split_policy_version": "v1",
            "policy_hash": H("policy:2"),
            "manifest_hash": H("ss_manifest:2"),
            "registry_snapshot_hash": H("ss_reg:2"),
            "ancestor_v1_dataset_registry_hash": H("ancestor:1"),
            "parent_registry_snapshot_hash": H("ss_reg:1"),
            "parent_manifest_hash": H("ss_manifest:1"),
            "parent_snapshot_id": ss1,
            "parent_epoch": 1,
            "transition_count": 0,
            "sample_count": 3,
            "count_train": 1,
            "count_validation": 1,
            "count_test": 1,
        },
    )

    sa, sb = U("snap:A"), U("snap:B")
    sa_snap, sa_split, sa_reg = H("snapA"), H("splitA"), H("regA")
    sb_snap, sb_split, sb_reg = H("snapB"), H("splitB"), H("regB")
    _insert(
        conn,
        "profiling",
        "profile_snapshots",
        {
            "id": sa,
            "epoch": 1,
            "split_snapshot_id": ss1,
            "split_manifest_hash": sa_split,
            "registry_snapshot_hash": sa_reg,
            "member_count": 6,
            "snapshot_hash": sa_snap,
        },
    )
    _insert(
        conn,
        "profiling",
        "profile_snapshots",
        {
            "id": sb,
            "epoch": 2,
            "split_snapshot_id": ss2,
            "split_manifest_hash": sb_split,
            "registry_snapshot_hash": sb_reg,
            "member_count": 3,
            "snapshot_hash": sb_snap,
        },
    )

    fsa, fsb = U("fs:A"), U("fs:B")
    fsa_hash, fsa_reg = H("fsA_hash"), H("feature_registry")
    fsb_hash, fsb_reg = H("fsB_hash"), H("feature_registry")  # share the L2-A registry hash
    for fid, fhash, freg in ((fsa, fsa_hash, fsa_reg), (fsb, fsb_hash, fsb_reg)):
        _insert(
            conn,
            "profiling",
            "feature_sets",
            {
                "id": fid,
                "feature_set_hash": fhash,
                "registry_hash": freg,
                "column_count": 3,
                "column_manifest": "[]",
            },
            jsonb_cols=("column_manifest",),
        )

    ma, mb, mv = U("mat:A"), U("mat:B"), U("mat:V")
    ma_hash, mb_hash, mv_hash = H("matA"), H("matB"), H("matV")
    for mid, snap, part, fset, mhash in (
        (ma, sa, "train", fsa, ma_hash),
        (mb, sb, "train", fsa, mb_hash),
        (mv, sa, "validation", fsa, mv_hash),
    ):
        _insert(
            conn,
            "profiling",
            "feature_matrices",
            {
                "id": mid,
                "profile_snapshot_id": snap,
                "partition": part,
                "feature_set_id": fset,
                "matrix_hash": mhash,
                "artifact_sha256": H(f"mat_artifact:{mid}"),
                "matrix_artifact_id": agen,
                "row_count": 2,
                "column_count": 3,
            },
        )

    psm: dict[tuple[str, str], str] = {}
    snapshot_members = {
        ("SA", "D1"): "train",
        ("SA", "D4"): "train",
        ("SA", "D7"): "train",
        ("SA", "D9"): "train",
        ("SA", "D2"): "validation",
        ("SA", "D3"): "test",
        ("SB", "D5"): "train",
        ("SB", "D6"): "train",
        ("SB", "D8"): "train",
    }
    snap_id = {"SA": sa, "SB": sb}
    for (slabel, d), part in snapshot_members.items():
        mid = U(f"psm:{slabel}:{d}")
        psm[(slabel, d)] = mid
        _insert(
            conn,
            "profiling",
            "profile_snapshot_members",
            {
                "id": mid,
                "profile_snapshot_id": snap_id[slabel],
                "bam_profile_id": bam[d],
                "dataset_registry_id": dsr[d],
                "partition": part,
                "feature_values_hash": fvh[d],
            },
        )

    fmm: dict[tuple[str, str], str] = {}
    fmm_index: dict[tuple[str, str], int] = {}
    matrix_members = {
        ("MA", "D1"): 0,
        ("MA", "D4"): 1,
        ("MA", "D7"): 2,
        ("MA", "D2"): 3,
        ("MA", "D3"): 4,
        ("MA", "D8"): 5,
        ("MB", "D5"): 10,
        ("MB", "D6"): 11,
        ("MB", "D9"): 12,
    }
    mat_id = {"MA": ma, "MB": mb}
    for (mlabel, d), midx in matrix_members.items():
        mmid = U(f"fmm:{mlabel}:{d}")
        fmm[(mlabel, d)] = mmid
        fmm_index[(mlabel, d)] = midx
        _insert(
            conn,
            "profiling",
            "feature_matrix_members",
            {
                "id": mmid,
                "feature_matrix_id": mat_id[mlabel],
                "dataset_registry_id": dsr[d],
                "member_index": midx,
                "vector_hash": H(f"vec:{mlabel}:{d}"),
                "feature_values_hash": fvh[d],
            },
        )

    ch = {
        "CH1": H("cfg1"),
        "CH2": H("cfg2"),
        "CH3": H("cfg3"),
        "CH4": H("cfg4"),
        "SM": H("cfg_sm"),
        "WM": H("cfg_wm"),
    }
    ar = {
        "CH1": U("ar:1"),
        "CH2": U("ar:2"),
        "CH3": U("ar:3"),
        "CH4": U("ar:4"),
        "SM": U("ar:sm"),
        "WM": U("ar:wm"),
    }
    for lbl in ("CH1", "CH2", "CH3", "CH4", "SM"):
        _insert(
            conn,
            "catalog",
            "artifacts",
            {
                "id": ar[lbl],
                "uri": f"mem://{lbl}",
                "sha256": ch[lbl],
                "media_type": CONFIG_MEDIA_TYPE,
            },
        )
    _insert(
        conn,
        "catalog",
        "artifacts",
        {"id": ar["WM"], "uri": "mem://wm", "sha256": ch["WM"], "media_type": _OTHER_MEDIA_TYPE},
    )

    conn.execute(text("RESET ROLE"))
    return UpstreamRefs(
        sa=sa,
        sb=sb,
        sa_snap=sa_snap,
        sa_split=sa_split,
        sa_reg=sa_reg,
        sb_snap=sb_snap,
        sb_split=sb_split,
        sb_reg=sb_reg,
        fsa=fsa,
        fsa_hash=fsa_hash,
        fsa_reg=fsa_reg,
        fsb=fsb,
        fsb_hash=fsb_hash,
        fsb_reg=fsb_reg,
        ma=ma,
        ma_hash=ma_hash,
        mb=mb,
        mb_hash=mb_hash,
        mv=mv,
        mv_hash=mv_hash,
        dsr=dsr,
        bam=bam,
        fvh=fvh,
        psm=psm,
        fmm=fmm,
        fmm_index=fmm_index,
        agen=agen,
        ar=ar,
        ch=ch,
        ps_a=ps_a,
        ps_b=ps_b,
        grh=grh,
        epp=epp,
    )


def seed_l2f_graph(conn: Connection, up: UpstreamRefs) -> SeededGraph:
    """Insert only the five ``0006`` L2-F tables against ``up``; return the full graph."""
    conn.execute(text("SET ROLE minos_admin"))
    ch, ar, ps_a, ps_b = up.ch, up.ar, up.ps_a, up.ps_b

    cp1, cp2, cp3, cp4 = U("cp:1"), U("cp:2"), U("cp:3"), U("cp:4")
    for cid, chash, psh, arid in (
        (cp1, ch["CH1"], ps_a, ar["CH1"]),
        (cp2, ch["CH2"], ps_a, ar["CH2"]),
        (cp3, ch["CH3"], ps_b, ar["CH3"]),
        (cp4, ch["CH4"], ps_a, ar["CH4"]),
    ):
        _insert(
            conn,
            "experiments",
            "l2f_config_payloads",
            {
                "id": cid,
                "config_hash": chash,
                "parameter_space_hash": psh,
                "schema_version": CONFIG_SCHEMA_VERSION,
                "media_type": CONFIG_MEDIA_TYPE,
                "artifact_id": arid,
            },
        )

    pa, pb = U("plan:A"), U("plan:B")
    pa_hash, pb_hash = H("plan_A"), H("plan_B")
    _insert(
        conn,
        "experiments",
        "l2f_experiment_plans",
        {
            "id": pa,
            "profile_snapshot_id": up.sa,
            "train_feature_matrix_id": up.ma,
            "feature_set_id": up.fsa,
            "partition": "train",
            "snapshot_hash": up.sa_snap,
            "split_manifest_hash": up.sa_split,
            "registry_snapshot_hash": up.sa_reg,
            "train_matrix_hash": up.ma_hash,
            "train_feature_view_hash": H("tfv_A"),
            "feature_set_hash": up.fsa_hash,
            "feature_registry_hash": up.fsa_reg,
            "gatk_registry_hash": up.grh,
            "parameter_space_hash": ps_a,
            "experiment_parameter_policy_hash": up.epp,
            "candidate_set_hash": H("csh_A"),
            "train_member_count": 2,
            "candidate_count": 2,
            "logical_job_count": 4,
            "plan_hash": pa_hash,
        },
    )
    _insert(
        conn,
        "experiments",
        "l2f_experiment_plans",
        {
            "id": pb,
            "profile_snapshot_id": up.sb,
            "train_feature_matrix_id": up.mb,
            "feature_set_id": up.fsa,
            "partition": "train",
            "snapshot_hash": up.sb_snap,
            "split_manifest_hash": up.sb_split,
            "registry_snapshot_hash": up.sb_reg,
            "train_matrix_hash": up.mb_hash,
            "train_feature_view_hash": H("tfv_B"),
            "feature_set_hash": up.fsa_hash,
            "feature_registry_hash": up.fsa_reg,
            "gatk_registry_hash": up.grh,
            "parameter_space_hash": ps_b,
            "experiment_parameter_policy_hash": up.epp,
            "candidate_set_hash": H("csh_B"),
            "train_member_count": 1,
            "candidate_count": 1,
            "logical_job_count": 1,
            "plan_hash": pb_hash,
        },
    )

    pm_d1, pm_d4, pm_d5 = U("pm:A:D1"), U("pm:A:D4"), U("pm:B:D5")
    for mid, plan, snap, mat, slabel, mlabel, d in (
        (pm_d1, pa, up.sa, up.ma, "SA", "MA", "D1"),
        (pm_d4, pa, up.sa, up.ma, "SA", "MA", "D4"),
        (pm_d5, pb, up.sb, up.mb, "SB", "MB", "D5"),
    ):
        _insert(
            conn,
            "experiments",
            "l2f_experiment_plan_members",
            {
                "id": mid,
                "plan_id": plan,
                "profile_snapshot_id": snap,
                "feature_matrix_id": mat,
                "profile_snapshot_member_id": up.psm[(slabel, d)],
                "feature_matrix_member_id": up.fmm[(mlabel, d)],
                "bam_profile_id": up.bam[d],
                "dataset_registry_id": up.dsr[d],
                "partition": "train",
                "feature_values_hash": up.fvh[d],
                "member_index": up.fmm_index[(mlabel, d)],
            },
        )

    pc1, pc2, pcb = U("pc:A:1"), U("pc:A:2"), U("pc:B:1")
    for cid, plan, payload, chash, psh, cidx in (
        (pc1, pa, cp1, ch["CH1"], ps_a, 0),
        (pc2, pa, cp2, ch["CH2"], ps_a, 1),
        (pcb, pb, cp3, ch["CH3"], ps_b, 0),
    ):
        _insert(
            conn,
            "experiments",
            "l2f_experiment_plan_configs",
            {
                "id": cid,
                "plan_id": plan,
                "config_payload_id": payload,
                "config_hash": chash,
                "parameter_space_hash": psh,
                "config_index": cidx,
            },
        )

    j1, j2, j3, jb = U("job:1"), U("job:2"), U("job:3"), U("job:B")
    jk1, jk2, jk3, jkb = H("jk1"), H("jk2"), H("jk3"), H("jkB")
    for jid, plan, member, config, jkey in (
        (j1, pa, pm_d1, pc1, jk1),
        (j2, pa, pm_d1, pc2, jk2),
        (j3, pa, pm_d4, pc1, jk3),
        (jb, pb, pm_d5, pcb, jkb),
    ):
        _insert(
            conn,
            "experiments",
            "l2f_experiment_jobs",
            {
                "id": jid,
                "plan_id": plan,
                "plan_member_id": member,
                "plan_config_id": config,
                "job_key": jkey,
                "status": "PENDING",
            },
        )

    conn.execute(text("RESET ROLE"))
    return SeededGraph(
        up=up,
        cp1=cp1,
        cp2=cp2,
        cp3=cp3,
        cp4=cp4,
        pa=pa,
        pa_hash=pa_hash,
        pb=pb,
        pb_hash=pb_hash,
        pm_d1=pm_d1,
        pm_d4=pm_d4,
        pm_d5=pm_d5,
        pc1=pc1,
        pc2=pc2,
        pcb=pcb,
        j1=j1,
        j2=j2,
        j3=j3,
        jb=jb,
        jk1=jk1,
        jk2=jk2,
        jk3=jk3,
        jkb=jkb,
    )


def seed_valid_graph(conn: Connection) -> SeededGraph:
    """Insert the complete valid graph (upstream + L2-F) and return its identifiers.

    Must be called on a connection already upgraded to ``0006`` inside an open transaction.
    """
    return seed_l2f_graph(conn, seed_upstream_graph(conn))


# expected seeded row counts (asserted by callers)
EXPECTED_ROW_COUNTS = {
    "l2f_experiment_plans": 2,
    "l2f_experiment_plan_members": 3,
    "l2f_config_payloads": 4,
    "l2f_experiment_plan_configs": 3,
    "l2f_experiment_jobs": 4,
}

# expected upstream seeded row counts (asserted by the populated lifecycle test)
EXPECTED_UPSTREAM_COUNTS = {
    ("catalog", "dataset_registry"): 9,
    ("profiling", "bam_profiles"): 9,
    ("catalog", "split_snapshots"): 2,
    ("profiling", "profile_snapshots"): 2,
    ("profiling", "feature_sets"): 2,
    ("profiling", "feature_matrices"): 3,
    ("profiling", "profile_snapshot_members"): 9,
    ("profiling", "feature_matrix_members"): 9,
}
