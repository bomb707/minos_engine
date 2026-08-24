"""L2-F2-A corrective — XOR serialization, metrics artifact identity, narrow registrar.

``0009`` is already pushed and this repository treats a pushed migration's bytes as history, so
every correction here is ADDITIVE and reversible rather than a rewrite of ``0009``.

Three demonstrated production defects close here:

* **XOR serialization.** ``0009``'s exclusive-outcome trigger took a ``FOR SHARE`` lock on the
  execution result before checking the opposite outcome table. Two transactions may hold SHARE
  simultaneously, so both could observe "no other outcome" and both commit — a success *and* a
  failure for one ``(execution, scoring contract)``. The lock becomes ``FOR UPDATE``, which is
  exclusive, so the second transaction blocks until the first commits and then genuinely sees it.
* **Metrics artifact identity.** The evaluation row recorded ``metrics_artifact_id``,
  ``metrics_artifact_sha256`` and ``metrics_media_type`` but bound only the id to
  ``catalog.artifacts``. A caller could therefore pair artifact A's id with artifact B's digest
  and PostgreSQL would accept it. The three columns become ONE composite foreign key, and the
  media type is pinned by CHECK to the L2-F2 metrics document type.
* **Registration path.** ``minos_evaluator`` deliberately has no ``INSERT`` on
  ``catalog.artifacts``, so the service principal had no way to register the metrics document it
  publishes without a privileged helper. A narrow ``SECURITY DEFINER`` registrar accepts only a
  digest, URI and size; media type and provenance are fixed inside the function and cannot be
  supplied by the caller.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_l2f2_evaluation_corrective"
down_revision: str | None = "0009_l2f_evaluation_results"
branch_labels = None
depends_on = None

_SCHEMA = "evaluation"
_RESULTS_TABLE = "l2f_evaluation_results"
_RESULTS = f"{_SCHEMA}.{_RESULTS_TABLE}"
_FAILURES = f"{_SCHEMA}.l2f_evaluation_failures"

_EXCLUSIVE = "evaluation.l2f_evaluation_exclusive_outcome"
_REGISTER_METRICS = "evaluation.l2f_register_metrics_artifact"

#: the ONLY media type an L2-F2 evaluation may cite, fixed in both the CHECK and the registrar.
_METRICS_MEDIA_TYPE = "application/vnd.minos.l2f2-evaluation-metrics+json"

#: mirrors the L2-F1 provenance vocabulary (``l2f:gatk-vcf``, ``l2f:execution-result-json``).
_METRICS_PROVENANCE = "l2f2:evaluation-metrics"

_ARTIFACT_COMPOSITE = "uq_artifacts_id_sha256_media"
_METRICS_FK = "fk_l2f_eval_results_metrics_artifact"
_METRICS_MEDIA_CK = "ck_l2f_eval_results_metrics_media"

_SQLSTATE_CONFLICT = "23505"
_SQLSTATE_INVALID = "22023"

_DENIED_ROLES = ("minos_live", "minos_runner", "minos_trainer")

_REGISTER_SIGNATURE = f"{_REGISTER_METRICS}(char, text, integer)"


def _exclusive_outcome_body(*, lock: str) -> str:
    """The exclusive-outcome trigger, parameterised only by its row-lock strength.

    ``0009`` shipped ``FOR SHARE``; the corrective ships ``FOR UPDATE``. Keeping one body means
    the downgrade restores ``0009``'s behaviour exactly rather than approximating it.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_EXCLUSIVE}() RETURNS trigger LANGUAGE plpgsql AS $excl$ "
        "DECLARE v_other integer; BEGIN "
        "PERFORM 1 FROM experiments.l2f_execution_results r "
        f"  WHERE r.id = NEW.execution_result_id {lock}; "
        "IF TG_TABLE_NAME = 'l2f_evaluation_results' THEN "
        f"  SELECT count(*) INTO v_other FROM {_FAILURES} f "
        "    WHERE f.execution_result_id = NEW.execution_result_id "
        "      AND f.scoring_contract_hash = NEW.scoring_contract_hash; "
        "  IF v_other > 0 THEN "
        "    RAISE EXCEPTION 'evaluation % already failed under this scoring contract', "
        "      NEW.execution_result_id USING ERRCODE = "
        "      '23514'; "
        "  END IF; "
        "ELSE "
        f"  SELECT count(*) INTO v_other FROM {_RESULTS} e "
        "    WHERE e.execution_result_id = NEW.execution_result_id "
        "      AND e.scoring_contract_hash = NEW.scoring_contract_hash; "
        "  IF v_other > 0 THEN "
        "    RAISE EXCEPTION 'evaluation % already succeeded under this scoring contract', "
        "      NEW.execution_result_id USING ERRCODE = "
        "      '23514'; "
        "  END IF; "
        "END IF; "
        "RETURN NEW; END; $excl$;"
    )


def _create_registrar() -> None:
    """The evaluator's ONLY path into ``catalog.artifacts``.

    Media type and provenance are constants inside the function: the caller supplies content
    identity (digest, URI, size) and nothing that could reclassify a document as some other kind
    of artifact. An exact re-registration returns the existing row so a crashed evaluation can
    resume; the same digest with different metadata is a typed conflict, never a silent update.
    """
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_REGISTER_METRICS}("
        "p_sha256 char(64), p_uri text, p_size_bytes integer) "
        "RETURNS TABLE(artifact_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $reg$ "
        "DECLARE v_row record; v_id uuid; BEGIN "
        "IF p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$' THEN "
        "  RAISE EXCEPTION 'metrics artifact sha256 must be canonical lowercase hex' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "IF p_uri IS NULL OR length(btrim(p_uri)) = 0 THEN "
        "  RAISE EXCEPTION 'metrics artifact uri must be non-empty' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "IF p_size_bytes IS NULL OR p_size_bytes < 0 THEN "
        "  RAISE EXCEPTION 'metrics artifact size_bytes must be non-negative' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "SELECT * INTO v_row FROM catalog.artifacts a WHERE a.sha256 = p_sha256; "
        "IF FOUND THEN "
        "  IF v_row.uri IS DISTINCT FROM p_uri "
        f"     OR v_row.media_type IS DISTINCT FROM '{_METRICS_MEDIA_TYPE}' "
        "     OR v_row.size_bytes IS DISTINCT FROM p_size_bytes "
        f"     OR v_row.provenance IS DISTINCT FROM '{_METRICS_PROVENANCE}' THEN "
        "    RAISE EXCEPTION 'artifact % is already registered with different metadata', "
        "      p_sha256 "
        f"      USING ERRCODE = '{_SQLSTATE_CONFLICT}'; END IF; "
        "  RETURN QUERY SELECT v_row.id, false; RETURN; END IF; "
        "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
        f"VALUES (p_uri, p_sha256, '{_METRICS_MEDIA_TYPE}', p_size_bytes, "
        f"        '{_METRICS_PROVENANCE}') "
        "RETURNING id INTO v_id; "
        "RETURN QUERY SELECT v_id, true; END; $reg$;"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_REGISTER_SIGNATURE} FROM PUBLIC;")
    for role in _DENIED_ROLES:
        op.execute(f"REVOKE ALL ON FUNCTION {_REGISTER_SIGNATURE} FROM {role};")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REGISTER_SIGNATURE} TO minos_evaluator;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REGISTER_SIGNATURE} TO minos_admin;")


def upgrade() -> None:
    # 1. exclusive (not shared) serialization of the success/failure XOR.
    op.execute(_exclusive_outcome_body(lock="FOR UPDATE"))

    # 2. the metrics artifact's id, digest and media type become ONE declarative identity.
    op.create_unique_constraint(
        _ARTIFACT_COMPOSITE, "artifacts", ["id", "sha256", "media_type"], schema="catalog"
    )
    op.drop_constraint(_METRICS_FK, _RESULTS_TABLE, schema=_SCHEMA, type_="foreignkey")
    op.create_foreign_key(
        _METRICS_FK,
        _RESULTS_TABLE,
        "artifacts",
        ["metrics_artifact_id", "metrics_artifact_sha256", "metrics_media_type"],
        ["id", "sha256", "media_type"],
        source_schema=_SCHEMA,
        referent_schema="catalog",
    )
    op.create_check_constraint(
        _METRICS_MEDIA_CK,
        _RESULTS_TABLE,
        f"metrics_media_type = '{_METRICS_MEDIA_TYPE}'",
        schema=_SCHEMA,
    )

    # 3. the narrow registrar the evaluator service principal actually uses.
    _create_registrar()


def downgrade() -> None:
    """Restore 0009 exactly: same trigger behaviour, same constraint inventory, no registrar."""
    op.execute(f"DROP FUNCTION IF EXISTS {_REGISTER_SIGNATURE};")

    op.drop_constraint(_METRICS_MEDIA_CK, _RESULTS_TABLE, schema=_SCHEMA, type_="check")
    op.drop_constraint(_METRICS_FK, _RESULTS_TABLE, schema=_SCHEMA, type_="foreignkey")
    op.create_foreign_key(
        _METRICS_FK,
        _RESULTS_TABLE,
        "artifacts",
        ["metrics_artifact_id"],
        ["id"],
        source_schema=_SCHEMA,
        referent_schema="catalog",
    )
    op.drop_constraint(_ARTIFACT_COMPOSITE, "artifacts", schema="catalog", type_="unique")

    op.execute(_exclusive_outcome_body(lock="FOR SHARE"))
