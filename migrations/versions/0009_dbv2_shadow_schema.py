"""DB-V2 D2 — the complete empty DB-V2 shadow schema (additive to 0008).

Creates the seven ``dbv2_*`` shadow schemas and every object the frozen DB-V2 contracts declare:
37 tables, 37 functions, 89 triggers and the 810-record D2 physical ACL. No V1
object is created, renamed, altered, dropped or written, and no data is moved: the shadow schema
is created EMPTY.

The migration is self-contained. It reads no report, calls no generator and touches no filesystem
or network; the SQL below is the complete executable result of
``scripts/gen_dbv2_migration.py``, which is a pure function of the three committed contracts:

    logical  68be636de5b9f85bc6bf051bf42a78f8cc6a72b774c17f36aacd696ac628ae2d
    physical 4b8e0525f0f688d5bdc01f85664c54183c2a8b4685b34abf153aff819a927b1e
    api      03aa18cef09b4ab7f9fb58313bada7e2212fd2ba00a06ae43ad47f9548a84e91

Role handling. PostgreSQL roles are CLUSTER objects, so this migration creates, alters and drops
none of them. It preflights: it records the original migration identity, requires all nine roles
to exist with their declared LOGIN/NOLOGIN configuration, requires the migration identity to be a
member of the NOLOGIN definer principal, and requires that no required role holds SUPERUSER,
CREATEROLE or CREATEDB. Every one of those checks runs BEFORE the first CREATE, ALTER or GRANT.
Only then does it elevate with ``SET LOCAL ROLE`` — never ``SET ROLE`` — which the transaction
undoes automatically at commit or abort. No RESET ROLE is issued, and not needing one is the
safety boundary.

Grant scope. Only objects this migration itself creates are granted or revoked. Nothing touches
``public``, ``public.alembic_version``, the database, or any existing V1 object.

Downgrade drops exactly the seven shadow schemas and nothing else. No cluster role is dropped or
altered, and no V1 object is affected.
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_dbv2_shadow_schema"
down_revision: str | None = "0008_l2f_execution_results"
branch_labels = None
depends_on = None

#: every statement of the forward migration, in execution order.
UPGRADE = (
    r"""DO $preflight$
DECLARE
    acting_session text := session_user;
    acting_current text := current_user;
    missing text;
    offender text;
BEGIN
    -- 1. the ORIGINAL migration identity, recorded before any elevation
    RAISE NOTICE 'dbv2 preflight: session_user=% current_user=%',
        acting_session, acting_current;
    -- 2. every required role exists
    SELECT r INTO missing FROM unnest(ARRAY[
        'minos_enqueue',
        'minos_evaluator',
        'minos_live',
        'minos_migrate',
        'minos_owner',
        'minos_planner',
        'minos_runner',
        'minos_trainer',
        'minos_verifier'
    ]) AS r
        WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) LIMIT 1;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'required role % does not exist; 0009 creates no cluster role',
            missing USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 3. the declared LOGIN/NOLOGIN configuration
    SELECT rolname INTO offender FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'minos_enqueue',
            'minos_evaluator',
            'minos_live',
            'minos_migrate',
            'minos_planner',
            'minos_runner',
            'minos_trainer',
            'minos_verifier'
        ]) AND NOT rolcanlogin LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'role % must be LOGIN', offender
            USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    SELECT rolname INTO offender FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'minos_owner'
        ]) AND rolcanlogin LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'role % must be NOLOGIN', offender
            USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 4. the migration identity is a member of the NOLOGIN definer principal
    IF NOT pg_has_role(acting_session, 'minos_owner', 'MEMBER') THEN
        RAISE EXCEPTION 'migration identity % is not a member of minos_owner',
            acting_session USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 5. no required role carries a cluster-wide privilege
    SELECT rolname INTO offender FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'minos_enqueue',
            'minos_evaluator',
            'minos_live',
            'minos_migrate',
            'minos_owner',
            'minos_planner',
            'minos_runner',
            'minos_trainer',
            'minos_verifier'
        ]) AND (rolsuper OR rolcreaterole OR rolcreatedb) LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'role % must not hold SUPERUSER, CREATEROLE or CREATEDB',
            offender USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 6. the definer principal may create schemas in THIS database
    IF NOT has_database_privilege('minos_owner', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'minos_owner may not create schemas in %; provision the database grant before migrating', current_database()
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- the shared Alembic table is verified, never altered
    PERFORM 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'alembic_version'
          AND c.relkind = 'r';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'public.alembic_version is missing'
            USING ERRCODE = 'undefined_table';
    END IF;
END
$preflight$;""",
    r"""SET LOCAL ROLE minos_owner;""",
    r"""DO $elevated$
BEGIN
    IF current_user <> 'minos_owner' THEN
        RAISE EXCEPTION 'elevation failed: current_user is %, expected minos_owner',
            current_user USING ERRCODE = 'invalid_authorization_specification';
    END IF;
END
$elevated$;""",
    r"""CREATE SCHEMA dbv2_audit AUTHORIZATION minos_owner;""",
    r"""CREATE SCHEMA dbv2_catalog AUTHORIZATION minos_owner;""",
    r"""CREATE SCHEMA dbv2_evaluation AUTHORIZATION minos_owner;""",
    r"""CREATE SCHEMA dbv2_experiments AUTHORIZATION minos_owner;""",
    r"""CREATE SCHEMA dbv2_models AUTHORIZATION minos_owner;""",
    r"""CREATE SCHEMA dbv2_profiling AUTHORIZATION minos_owner;""",
    r"""CREATE SCHEMA dbv2_runtime AUTHORIZATION minos_owner;""",
    r"""CREATE TABLE dbv2_audit.events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    actor_role text NOT NULL,
    action text NOT NULL,
    object_schema text NOT NULL,
    object_table text NOT NULL,
    object_id uuid NULL,
    payload_hash char(64) NULL,
    occurred_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_audit_events PRIMARY KEY (id),
    CONSTRAINT ck_audit_events_payload_hex CHECK (payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_audit_events_action CHECK (length(action) > 0)
);""",
    r"""CREATE INDEX ix_audit_events_time ON dbv2_audit.events USING btree (occurred_at);""",
    r"""CREATE INDEX ix_audit_events_object ON dbv2_audit.events USING btree (object_schema, object_table, object_id);""",
    r"""CREATE TABLE dbv2_catalog.artifacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    artifact_kind text NOT NULL,
    content_sha256 char(64) NOT NULL,
    size_bytes bigint NOT NULL,
    media_type text NOT NULL,
    schema_version text NULL,
    storage_mode text NOT NULL,
    inline_payload bytea NULL,
    lifecycle_state text DEFAULT 'active' NOT NULL,
    retention_class text NOT NULL,
    backup_scope text DEFAULT 'operational' NOT NULL,
    provenance jsonb NOT NULL,
    first_verified_at timestamptz NULL,
    last_verified_at timestamptz NULL,
    verification_state text DEFAULT 'unverified' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_artifacts PRIMARY KEY (id),
    CONSTRAINT uq_artifacts_content_sha256 UNIQUE (content_sha256),
    CONSTRAINT uq_artifacts_id_sha_media UNIQUE (id, content_sha256, media_type),
    CONSTRAINT ck_artifacts_backup_scope CHECK (backup_scope IN ('operational','recovery')),
    CONSTRAINT ck_artifacts_inline_bounded CHECK ((storage_mode = 'inline' AND inline_payload IS NOT NULL AND size_bytes = octet_length(inline_payload) AND content_sha256 = encode(sha256(inline_payload), 'hex') AND size_bytes <= 65536) OR (storage_mode = 'external' AND inline_payload IS NULL)),
    CONSTRAINT ck_artifacts_kind CHECK (length(artifact_kind) > 0),
    CONSTRAINT ck_artifacts_lifecycle CHECK (lifecycle_state IN ('active','archived','quarantined','deleted')),
    CONSTRAINT ck_artifacts_retention CHECK (retention_class IN ('permanent','long','standard','ephemeral')),
    CONSTRAINT ck_artifacts_sha_hex CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_artifacts_size_nonneg CHECK (size_bytes >= 0),
    CONSTRAINT ck_artifacts_storage_mode CHECK (storage_mode IN ('external','inline')),
    CONSTRAINT ck_artifacts_verification CHECK (verification_state IN ('unverified','verified','missing','corrupt'))
);""",
    r"""CREATE INDEX ix_artifacts_kind_created ON dbv2_catalog.artifacts USING btree (artifact_kind, created_at);""",
    r"""CREATE INDEX ix_artifacts_needs_verification ON dbv2_catalog.artifacts USING btree (last_verified_at) WHERE lifecycle_state = 'active';""",
    r"""CREATE INDEX ix_artifacts_operational_snapshot ON dbv2_catalog.artifacts USING btree (content_sha256, size_bytes, artifact_kind) WHERE lifecycle_state = 'active' AND backup_scope = 'operational';""",
    r"""CREATE TABLE dbv2_catalog.backup_sets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    backup_key text NOT NULL,
    recovery_set_id uuid NOT NULL,
    alembic_revision text NOT NULL,
    quiesce_started_at timestamptz NOT NULL,
    quiesce_ended_at timestamptz NOT NULL,
    manifest_schema_version text NOT NULL,
    database_name text NOT NULL,
    recovery_manifest_artifact_id uuid NOT NULL,
    recovery_manifest_sha256 char(64) NOT NULL,
    recovery_manifest_media_type text DEFAULT 'application/vnd.minos.db-recovery-manifest+json' NOT NULL,
    database_backup_kind text NOT NULL,
    database_backup_artifact_id uuid NOT NULL,
    database_backup_sha256 char(64) NOT NULL,
    database_backup_media_type text DEFAULT 'application/vnd.postgresql.dump' NOT NULL,
    database_backup_size_bytes bigint NOT NULL,
    wal_start_lsn text NULL,
    wal_end_lsn text NULL,
    artifact_snapshot_manifest_artifact_id uuid NULL,
    artifact_snapshot_manifest_sha256 char(64) NULL,
    artifact_snapshot_sha256 char(64) NULL,
    artifact_snapshot_manifest_media_type text NULL,
    artifact_count bigint NULL,
    artifact_total_bytes bigint NULL,
    postgresql_version text NOT NULL,
    backup_tool_version text NOT NULL,
    artifact_verification_tool_version text NOT NULL,
    completeness text NOT NULL,
    restore_tested_at timestamptz NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_backup_sets PRIMARY KEY (id),
    CONSTRAINT uq_backup_sets_key UNIQUE (backup_key),
    CONSTRAINT uq_backup_sets_recovery_set UNIQUE (recovery_set_id),
    CONSTRAINT uq_backup_sets_manifest UNIQUE (recovery_manifest_sha256),
    CONSTRAINT ck_backup_sets_artifact_hex CHECK ((artifact_snapshot_sha256 IS NULL OR artifact_snapshot_sha256 ~ '^[0-9a-f]{64}$') AND (artifact_snapshot_manifest_sha256 IS NULL OR artifact_snapshot_manifest_sha256 ~ '^[0-9a-f]{64}$')),
    CONSTRAINT ck_backup_sets_completeness CHECK (completeness IN ('complete','database_only')),
    CONSTRAINT ck_backup_sets_db_hex CHECK (database_backup_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_backup_sets_db_media CHECK (database_backup_media_type = 'application/vnd.postgresql.dump'),
    CONSTRAINT ck_backup_sets_distinct_artifacts CHECK (recovery_manifest_artifact_id <> database_backup_artifact_id AND (artifact_snapshot_manifest_artifact_id IS NULL OR (recovery_manifest_artifact_id <> artifact_snapshot_manifest_artifact_id AND database_backup_artifact_id <> artifact_snapshot_manifest_artifact_id))),
    CONSTRAINT ck_backup_sets_identity_nonempty CHECK (length(manifest_schema_version) > 0 AND length(database_name) > 0),
    CONSTRAINT ck_backup_sets_kind CHECK (database_backup_kind IN ('pg_dump','basebackup')),
    CONSTRAINT ck_backup_sets_manifest_hex CHECK (recovery_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_backup_sets_manifest_media CHECK (recovery_manifest_media_type = 'application/vnd.minos.db-recovery-manifest+json'),
    CONSTRAINT ck_backup_sets_quiesce_window CHECK (quiesce_ended_at >= quiesce_started_at),
    CONSTRAINT ck_backup_sets_shape CHECK ((completeness = 'complete' AND artifact_snapshot_manifest_artifact_id IS NOT NULL AND artifact_snapshot_manifest_sha256 IS NOT NULL AND artifact_snapshot_sha256 IS NOT NULL AND artifact_snapshot_manifest_media_type IS NOT NULL AND artifact_count IS NOT NULL AND artifact_total_bytes IS NOT NULL AND artifact_count >= 0 AND artifact_total_bytes >= 0) OR (completeness = 'database_only' AND artifact_snapshot_manifest_artifact_id IS NULL AND artifact_snapshot_manifest_sha256 IS NULL AND artifact_snapshot_sha256 IS NULL AND artifact_snapshot_manifest_media_type IS NULL AND artifact_count IS NULL AND artifact_total_bytes IS NULL)),
    CONSTRAINT ck_backup_sets_snapshot_identities_differ CHECK (artifact_snapshot_sha256 IS NULL OR artifact_snapshot_sha256 <> artifact_snapshot_manifest_sha256),
    CONSTRAINT ck_backup_sets_snapshot_media CHECK (artifact_snapshot_manifest_media_type IS NULL OR artifact_snapshot_manifest_media_type = 'application/vnd.minos.artifact-snapshot+json'),
    CONSTRAINT fk_backup_sets_artifact_snapshot_manifest FOREIGN KEY (artifact_snapshot_manifest_artifact_id, artifact_snapshot_manifest_sha256, artifact_snapshot_manifest_media_type) REFERENCES dbv2_catalog.artifacts (id, content_sha256, media_type),
    CONSTRAINT fk_backup_sets_database_backup FOREIGN KEY (database_backup_artifact_id, database_backup_sha256, database_backup_media_type) REFERENCES dbv2_catalog.artifacts (id, content_sha256, media_type),
    CONSTRAINT fk_backup_sets_recovery_manifest FOREIGN KEY (recovery_manifest_artifact_id, recovery_manifest_sha256, recovery_manifest_media_type) REFERENCES dbv2_catalog.artifacts (id, content_sha256, media_type)
);""",
    r"""CREATE INDEX ix_backup_sets_created ON dbv2_catalog.backup_sets USING btree (created_at);""",
    r"""CREATE TABLE dbv2_audit.admin_operations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    operation_kind text NOT NULL,
    alembic_revision_from text NULL,
    alembic_revision_to text NULL,
    backup_set_id uuid NULL,
    outcome text NOT NULL,
    evidence_hash char(64) NULL,
    occurred_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_admin_operations PRIMARY KEY (id),
    CONSTRAINT ck_admin_operations_kind CHECK (operation_kind IN ('migration','restore','restore_drill','cutover','rollback')),
    CONSTRAINT ck_admin_operations_outcome CHECK (outcome IN ('succeeded','failed','aborted')),
    CONSTRAINT fk_admin_operations_backup FOREIGN KEY (backup_set_id) REFERENCES dbv2_catalog.backup_sets (id)
);""",
    r"""CREATE INDEX ix_admin_operations_time ON dbv2_audit.admin_operations USING btree (occurred_at);""",
    r"""CREATE TABLE dbv2_catalog.datasets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dataset_key text NOT NULL,
    round_id text NOT NULL,
    chromosome text NOT NULL,
    region_source text NOT NULL,
    region_start0 bigint NOT NULL,
    region_end0_exclusive bigint NOT NULL,
    region_coordinate_system text NOT NULL,
    region_hash char(64) NOT NULL,
    bam_artifact_id uuid NOT NULL,
    bai_artifact_id uuid NOT NULL,
    reference_artifact_id uuid NOT NULL,
    fai_artifact_id uuid NOT NULL,
    identity_hash char(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_datasets PRIMARY KEY (id),
    CONSTRAINT uq_datasets_key UNIQUE (dataset_key),
    CONSTRAINT uq_datasets_identity_hash UNIQUE (identity_hash),
    CONSTRAINT ck_datasets_region CHECK (region_end0_exclusive > region_start0),
    CONSTRAINT ck_datasets_region_hash_hex CHECK (region_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_datasets_identity_hex CHECK (identity_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_datasets_coord CHECK (region_coordinate_system = 'half_open_0_based'),
    CONSTRAINT fk_datasets_bam FOREIGN KEY (bam_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_datasets_bai FOREIGN KEY (bai_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_datasets_reference FOREIGN KEY (reference_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_datasets_fai FOREIGN KEY (fai_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE INDEX ix_datasets_round_chrom ON dbv2_catalog.datasets USING btree (round_id, chromosome);""",
    r"""CREATE TABLE dbv2_catalog.releases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    release_key text NOT NULL,
    release_hash char(64) NOT NULL,
    component_manifest jsonb NOT NULL,
    state text DEFAULT 'draft' NOT NULL,
    created_by_role text NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_releases PRIMARY KEY (id),
    CONSTRAINT uq_releases_key UNIQUE (release_key),
    CONSTRAINT uq_releases_hash UNIQUE (release_hash),
    CONSTRAINT ck_releases_hash_hex CHECK (release_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_releases_state CHECK (state IN ('draft','qualified','active','superseded'))
);""",
    r"""CREATE INDEX ix_releases_state ON dbv2_catalog.releases USING btree (state) WHERE state = 'active';""",
    r"""CREATE TABLE dbv2_catalog.storage_backends (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    backend_key text NOT NULL,
    backend_type text NOT NULL,
    logical_root text NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    is_read_only boolean DEFAULT false NOT NULL,
    notes text NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_storage_backends PRIMARY KEY (id),
    CONSTRAINT uq_storage_backends_key UNIQUE (backend_key),
    CONSTRAINT ck_storage_backends_type CHECK (backend_type IN ('local_fs','s3','minio')),
    CONSTRAINT ck_storage_backends_key_nonempty CHECK (length(backend_key) > 0),
    CONSTRAINT ck_storage_backends_root_nonempty CHECK (length(logical_root) > 0)
);""",
    r"""CREATE TABLE dbv2_catalog.artifact_locations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    artifact_id uuid NOT NULL,
    backend_id uuid NOT NULL,
    object_key text NOT NULL,
    location_state text DEFAULT 'present' NOT NULL,
    is_primary boolean DEFAULT true NOT NULL,
    last_verified_at timestamptz NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_artifact_locations PRIMARY KEY (id),
    CONSTRAINT uq_artifact_locations_backend_key UNIQUE (backend_id, object_key),
    CONSTRAINT uq_artifact_locations_artifact_backend UNIQUE (artifact_id, backend_id),
    CONSTRAINT ck_artifact_locations_state CHECK (location_state IN ('present','missing','corrupt','evacuated')),
    CONSTRAINT ck_artifact_locations_key_relative CHECK (object_key !~ '^/' AND object_key !~ '\.\.' AND length(object_key) > 0),
    CONSTRAINT fk_artifact_locations_artifact FOREIGN KEY (artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_artifact_locations_backend FOREIGN KEY (backend_id) REFERENCES dbv2_catalog.storage_backends (id)
);""",
    r"""CREATE INDEX ix_artifact_locations_artifact ON dbv2_catalog.artifact_locations USING btree (artifact_id);""",
    r"""CREATE INDEX ix_artifact_locations_unhealthy ON dbv2_catalog.artifact_locations USING btree (location_state) WHERE location_state <> 'present';""",
    r"""CREATE TABLE dbv2_evaluation.truth_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dataset_id uuid NOT NULL,
    truth_vcf_artifact_id uuid NOT NULL,
    truth_index_artifact_id uuid NULL,
    mutations_vcf_artifact_id uuid NULL,
    binding_hash char(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_truth_bindings PRIMARY KEY (id),
    CONSTRAINT uq_truth_bindings_dataset UNIQUE (dataset_id),
    CONSTRAINT ck_truth_bindings_hash_hex CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_truth_bindings_dataset FOREIGN KEY (dataset_id) REFERENCES dbv2_catalog.datasets (id),
    CONSTRAINT fk_truth_bindings_vcf FOREIGN KEY (truth_vcf_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_truth_bindings_index FOREIGN KEY (truth_index_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_truth_bindings_mut FOREIGN KEY (mutations_vcf_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE TABLE dbv2_experiments.parameter_spaces (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    space_key text NOT NULL,
    caller text NOT NULL,
    parameter_space_hash char(64) NOT NULL,
    definition_artifact_id uuid NOT NULL,
    source_retrieved_at timestamptz NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_parameter_spaces PRIMARY KEY (id),
    CONSTRAINT uq_parameter_spaces_key UNIQUE (space_key),
    CONSTRAINT uq_parameter_spaces_hash UNIQUE (parameter_space_hash),
    CONSTRAINT uq_parameter_spaces_id_hash UNIQUE (id, parameter_space_hash),
    CONSTRAINT ck_parameter_spaces_hash_hex CHECK (parameter_space_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_parameter_spaces_caller CHECK (caller = 'gatk'),
    CONSTRAINT fk_parameter_spaces_art FOREIGN KEY (definition_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE TABLE dbv2_experiments.candidate_configs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    parameter_space_id uuid NOT NULL,
    config_hash char(64) NOT NULL,
    parameter_space_hash char(64) NOT NULL,
    payload_artifact_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_candidate_configs PRIMARY KEY (id),
    CONSTRAINT uq_candidate_configs_hash UNIQUE (config_hash),
    CONSTRAINT uq_candidate_configs_id_hash UNIQUE (id, config_hash),
    CONSTRAINT ck_candidate_configs_hash_hex CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_candidate_configs_space FOREIGN KEY (parameter_space_id, parameter_space_hash) REFERENCES dbv2_experiments.parameter_spaces (id, parameter_space_hash),
    CONSTRAINT fk_candidate_configs_payload FOREIGN KEY (payload_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE INDEX ix_candidate_configs_space ON dbv2_experiments.candidate_configs USING btree (parameter_space_id);""",
    r"""CREATE TABLE dbv2_experiments.candidate_sets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    candidate_set_hash char(64) NOT NULL,
    parameter_space_id uuid NOT NULL,
    candidate_count integer NOT NULL,
    generator_version text NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_candidate_sets PRIMARY KEY (id),
    CONSTRAINT uq_candidate_sets_hash UNIQUE (candidate_set_hash),
    CONSTRAINT uq_candidate_sets_id_hash UNIQUE (id, candidate_set_hash),
    CONSTRAINT ck_candidate_sets_hash_hex CHECK (candidate_set_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_candidate_sets_count CHECK (candidate_count > 0),
    CONSTRAINT fk_candidate_sets_space FOREIGN KEY (parameter_space_id) REFERENCES dbv2_experiments.parameter_spaces (id)
);""",
    r"""CREATE TABLE dbv2_experiments.candidate_set_configs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    candidate_set_id uuid NOT NULL,
    candidate_config_id uuid NOT NULL,
    config_index integer NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_candidate_set_configs PRIMARY KEY (id),
    CONSTRAINT uq_csc_index UNIQUE (candidate_set_id, config_index),
    CONSTRAINT uq_csc_member UNIQUE (candidate_set_id, candidate_config_id),
    CONSTRAINT ck_csc_index CHECK (config_index >= 0),
    CONSTRAINT fk_csc_set FOREIGN KEY (candidate_set_id) REFERENCES dbv2_experiments.candidate_sets (id),
    CONSTRAINT fk_csc_config FOREIGN KEY (candidate_config_id) REFERENCES dbv2_experiments.candidate_configs (id)
);""",
    r"""CREATE TABLE dbv2_profiling.bam_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dataset_id uuid NOT NULL,
    profile_key text NOT NULL,
    profiler_version text NOT NULL,
    profiler_config_hash char(64) NOT NULL,
    profile_status text NOT NULL,
    integrity_degraded boolean DEFAULT false NOT NULL,
    windows_row_count bigint NOT NULL,
    eligible_value_count bigint NOT NULL,
    feature_values_hash char(64) NOT NULL,
    identity_hash char(64) NOT NULL,
    profile_artifact_id uuid NOT NULL,
    manifest_artifact_id uuid NOT NULL,
    windows_artifact_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_bam_profiles PRIMARY KEY (id),
    CONSTRAINT uq_bam_profiles_key UNIQUE (profile_key),
    CONSTRAINT uq_bam_profiles_dataset UNIQUE (dataset_id),
    CONSTRAINT uq_bam_profiles_identity UNIQUE (identity_hash),
    CONSTRAINT uq_bam_profiles_id_dataset UNIQUE (id, dataset_id),
    CONSTRAINT ck_bam_profiles_identity_hex CHECK (identity_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_bam_profiles_values_hex CHECK (feature_values_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_bam_profiles_status CHECK (profile_status IN ('accepted','rejected','degraded')),
    CONSTRAINT ck_bam_profiles_counts CHECK (windows_row_count >= 0 AND eligible_value_count >= 0),
    CONSTRAINT fk_bam_profiles_dataset FOREIGN KEY (dataset_id) REFERENCES dbv2_catalog.datasets (id),
    CONSTRAINT fk_bam_profiles_profile FOREIGN KEY (profile_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_bam_profiles_manifest FOREIGN KEY (manifest_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_bam_profiles_windows FOREIGN KEY (windows_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE INDEX ix_bam_profiles_dataset ON dbv2_profiling.bam_profiles USING btree (dataset_id);""",
    r"""CREATE TABLE dbv2_profiling.feature_sets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    feature_set_key text NOT NULL,
    feature_count integer NOT NULL,
    feature_set_hash char(64) NOT NULL,
    definition jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_feature_sets PRIMARY KEY (id),
    CONSTRAINT uq_feature_sets_key UNIQUE (feature_set_key),
    CONSTRAINT uq_feature_sets_hash UNIQUE (feature_set_hash),
    CONSTRAINT ck_feature_sets_hash_hex CHECK (feature_set_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_feature_sets_count CHECK (feature_count > 0)
);""",
    r"""CREATE TABLE dbv2_models.model_definitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_key text NOT NULL,
    feature_set_id uuid NOT NULL,
    model_kind text NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_model_definitions PRIMARY KEY (id),
    CONSTRAINT uq_model_definitions_key UNIQUE (model_key),
    CONSTRAINT ck_model_definitions_key CHECK (length(model_key) > 0),
    CONSTRAINT fk_model_definitions_fs FOREIGN KEY (feature_set_id) REFERENCES dbv2_profiling.feature_sets (id)
);""",
    r"""CREATE TABLE dbv2_profiling.profile_snapshots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshot_key text NOT NULL,
    epoch integer NOT NULL,
    parent_snapshot_id uuid NULL,
    split_algorithm_version text NOT NULL,
    member_count integer NOT NULL,
    snapshot_hash char(64) NOT NULL,
    state text DEFAULT 'frozen' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_profile_snapshots PRIMARY KEY (id),
    CONSTRAINT uq_profile_snapshots_key UNIQUE (snapshot_key),
    CONSTRAINT uq_profile_snapshots_hash UNIQUE (snapshot_hash),
    CONSTRAINT uq_profile_snapshots_epoch UNIQUE (epoch),
    CONSTRAINT ck_profile_snapshots_hash_hex CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_profile_snapshots_epoch CHECK (epoch >= 1),
    CONSTRAINT ck_profile_snapshots_state CHECK (state IN ('frozen','superseded')),
    CONSTRAINT ck_profile_snapshots_members CHECK (member_count > 0),
    CONSTRAINT fk_profile_snapshots_parent FOREIGN KEY (parent_snapshot_id) REFERENCES dbv2_profiling.profile_snapshots (id)
);""",
    r"""CREATE TABLE dbv2_experiments.experiment_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    plan_hash char(64) NOT NULL,
    snapshot_id uuid NOT NULL,
    candidate_set_id uuid NOT NULL,
    parameter_space_id uuid NOT NULL,
    partition text NOT NULL,
    member_count integer NOT NULL,
    candidate_count integer NOT NULL,
    logical_job_count bigint NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_experiment_plans PRIMARY KEY (id),
    CONSTRAINT uq_plans_hash UNIQUE (plan_hash),
    CONSTRAINT uq_plans_id_hash UNIQUE (id, plan_hash),
    CONSTRAINT uq_plans_scope UNIQUE (snapshot_id, candidate_set_id, partition),
    CONSTRAINT ck_plans_hash_hex CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_plans_partition CHECK (partition = 'train'),
    CONSTRAINT ck_plans_job_count CHECK (logical_job_count = member_count::bigint * candidate_count),
    CONSTRAINT fk_plans_snapshot FOREIGN KEY (snapshot_id) REFERENCES dbv2_profiling.profile_snapshots (id),
    CONSTRAINT fk_plans_candidate_set FOREIGN KEY (candidate_set_id) REFERENCES dbv2_experiments.candidate_sets (id),
    CONSTRAINT fk_plans_space FOREIGN KEY (parameter_space_id) REFERENCES dbv2_experiments.parameter_spaces (id)
);""",
    r"""CREATE TABLE dbv2_experiments.experiment_plan_configs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    plan_id uuid NOT NULL,
    candidate_config_id uuid NOT NULL,
    config_hash char(64) NOT NULL,
    config_index integer NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_experiment_plan_configs PRIMARY KEY (id),
    CONSTRAINT uq_plan_configs_index UNIQUE (plan_id, config_index),
    CONSTRAINT uq_plan_configs_config UNIQUE (plan_id, candidate_config_id),
    CONSTRAINT uq_plan_configs_id_plan UNIQUE (id, plan_id),
    CONSTRAINT ck_plan_configs_index CHECK (config_index >= 0),
    CONSTRAINT fk_plan_configs_plan FOREIGN KEY (plan_id) REFERENCES dbv2_experiments.experiment_plans (id),
    CONSTRAINT fk_plan_configs_config_identity FOREIGN KEY (candidate_config_id, config_hash) REFERENCES dbv2_experiments.candidate_configs (id, config_hash)
);""",
    r"""CREATE INDEX ix_plan_configs_plan ON dbv2_experiments.experiment_plan_configs USING btree (plan_id, config_index);""",
    r"""CREATE TABLE dbv2_profiling.feature_matrices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    matrix_key text NOT NULL,
    snapshot_id uuid NOT NULL,
    feature_set_id uuid NOT NULL,
    partition text NOT NULL,
    row_count integer NOT NULL,
    matrix_hash char(64) NOT NULL,
    matrix_artifact_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_feature_matrices PRIMARY KEY (id),
    CONSTRAINT uq_feature_matrices_key UNIQUE (matrix_key),
    CONSTRAINT uq_feature_matrices_hash UNIQUE (matrix_hash),
    CONSTRAINT uq_feature_matrices_scope UNIQUE (snapshot_id, feature_set_id, partition),
    CONSTRAINT ck_feature_matrices_hash_hex CHECK (matrix_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_feature_matrices_partition CHECK (partition IN ('train','validation','test')),
    CONSTRAINT ck_feature_matrices_rows CHECK (row_count > 0),
    CONSTRAINT fk_feature_matrices_snap FOREIGN KEY (snapshot_id) REFERENCES dbv2_profiling.profile_snapshots (id),
    CONSTRAINT fk_feature_matrices_set FOREIGN KEY (feature_set_id) REFERENCES dbv2_profiling.feature_sets (id),
    CONSTRAINT fk_feature_matrices_artifact FOREIGN KEY (matrix_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE TABLE dbv2_models.training_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_definition_id uuid NOT NULL,
    train_matrix_id uuid NOT NULL,
    validation_matrix_id uuid NULL,
    trainer_version text NOT NULL,
    training_identity_hash char(64) NOT NULL,
    state text DEFAULT 'running' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    completed_at timestamptz NULL,
    CONSTRAINT pk_training_runs PRIMARY KEY (id),
    CONSTRAINT uq_training_runs_identity UNIQUE (training_identity_hash),
    CONSTRAINT ck_training_runs_hash_hex CHECK (training_identity_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_training_runs_state CHECK (state IN ('running','complete','failed')),
    CONSTRAINT fk_training_runs_def FOREIGN KEY (model_definition_id) REFERENCES dbv2_models.model_definitions (id),
    CONSTRAINT fk_training_runs_train FOREIGN KEY (train_matrix_id) REFERENCES dbv2_profiling.feature_matrices (id),
    CONSTRAINT fk_training_runs_validation FOREIGN KEY (validation_matrix_id) REFERENCES dbv2_profiling.feature_matrices (id)
);""",
    r"""CREATE INDEX ix_training_runs_def ON dbv2_models.training_runs USING btree (model_definition_id, created_at);""",
    r"""CREATE TABLE dbv2_models.model_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_definition_id uuid NOT NULL,
    training_run_id uuid NOT NULL,
    version_key text NOT NULL,
    bundle_artifact_id uuid NOT NULL,
    model_hash char(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_model_versions PRIMARY KEY (id),
    CONSTRAINT uq_model_versions_key UNIQUE (model_definition_id, version_key),
    CONSTRAINT uq_model_versions_hash UNIQUE (model_hash),
    CONSTRAINT ck_model_versions_hash_hex CHECK (model_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_model_versions_def FOREIGN KEY (model_definition_id) REFERENCES dbv2_models.model_definitions (id),
    CONSTRAINT fk_model_versions_run FOREIGN KEY (training_run_id) REFERENCES dbv2_models.training_runs (id),
    CONSTRAINT fk_model_versions_bundle FOREIGN KEY (bundle_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE TABLE dbv2_models.model_activations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_version_id uuid NOT NULL,
    release_id uuid NULL,
    activated_at timestamptz DEFAULT now() NOT NULL,
    deactivated_at timestamptz NULL,
    activated_by_role text NOT NULL,
    reason text NULL,
    CONSTRAINT pk_model_activations PRIMARY KEY (id),
    CONSTRAINT ck_model_activations_window CHECK (deactivated_at IS NULL OR deactivated_at >= activated_at),
    CONSTRAINT fk_model_activations_ver FOREIGN KEY (model_version_id) REFERENCES dbv2_models.model_versions (id),
    CONSTRAINT fk_model_activations_release FOREIGN KEY (release_id) REFERENCES dbv2_catalog.releases (id)
);""",
    r"""CREATE INDEX ix_model_activations_current ON dbv2_models.model_activations USING btree (model_version_id) WHERE deactivated_at IS NULL;""",
    r"""CREATE UNIQUE INDEX uq_model_activations_single_active ON dbv2_models.model_activations USING btree (deactivated_at) WHERE deactivated_at IS NULL;""",
    r"""CREATE TABLE dbv2_profiling.feature_matrix_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    matrix_id uuid NOT NULL,
    bam_profile_id uuid NOT NULL,
    row_index integer NOT NULL,
    vector_hash char(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_feature_matrix_members PRIMARY KEY (id),
    CONSTRAINT uq_matrix_members_profile UNIQUE (matrix_id, bam_profile_id),
    CONSTRAINT uq_matrix_members_index UNIQUE (matrix_id, row_index),
    CONSTRAINT ck_matrix_members_vector_hex CHECK (vector_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_matrix_members_index CHECK (row_index >= 0),
    CONSTRAINT fk_matrix_members_matrix FOREIGN KEY (matrix_id) REFERENCES dbv2_profiling.feature_matrices (id),
    CONSTRAINT fk_matrix_members_profile FOREIGN KEY (bam_profile_id) REFERENCES dbv2_profiling.bam_profiles (id)
);""",
    r"""CREATE INDEX ix_matrix_members_matrix ON dbv2_profiling.feature_matrix_members USING btree (matrix_id, row_index);""",
    r"""CREATE TABLE dbv2_profiling.profile_snapshot_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshot_id uuid NOT NULL,
    bam_profile_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    partition text NOT NULL,
    member_index integer NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_profile_snapshot_members PRIMARY KEY (id),
    CONSTRAINT uq_snapshot_members_profile UNIQUE (snapshot_id, bam_profile_id),
    CONSTRAINT uq_snapshot_members_index UNIQUE (snapshot_id, partition, member_index),
    CONSTRAINT ck_snapshot_members_partition CHECK (partition IN ('train','validation','test')),
    CONSTRAINT ck_snapshot_members_index CHECK (member_index >= 0),
    CONSTRAINT fk_snapshot_members_snapshot FOREIGN KEY (snapshot_id) REFERENCES dbv2_profiling.profile_snapshots (id),
    CONSTRAINT fk_snapshot_members_profile_dataset FOREIGN KEY (bam_profile_id, dataset_id) REFERENCES dbv2_profiling.bam_profiles (id, dataset_id)
);""",
    r"""CREATE INDEX ix_snapshot_members_partition ON dbv2_profiling.profile_snapshot_members USING btree (snapshot_id, partition, member_index);""",
    r"""CREATE TABLE dbv2_experiments.experiment_plan_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    plan_id uuid NOT NULL,
    snapshot_member_id uuid NOT NULL,
    bam_profile_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    member_index integer NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_experiment_plan_members PRIMARY KEY (id),
    CONSTRAINT uq_plan_members_index UNIQUE (plan_id, member_index),
    CONSTRAINT uq_plan_members_member UNIQUE (plan_id, snapshot_member_id),
    CONSTRAINT uq_plan_members_id_plan UNIQUE (id, plan_id),
    CONSTRAINT ck_plan_members_index CHECK (member_index >= 0),
    CONSTRAINT fk_plan_members_plan FOREIGN KEY (plan_id) REFERENCES dbv2_experiments.experiment_plans (id),
    CONSTRAINT fk_plan_members_snapshot_member FOREIGN KEY (snapshot_member_id) REFERENCES dbv2_profiling.profile_snapshot_members (id),
    CONSTRAINT fk_plan_members_profile_dataset FOREIGN KEY (bam_profile_id, dataset_id) REFERENCES dbv2_profiling.bam_profiles (id, dataset_id)
);""",
    r"""CREATE INDEX ix_plan_members_plan ON dbv2_experiments.experiment_plan_members USING btree (plan_id, member_index);""",
    r"""CREATE TABLE dbv2_experiments.experiment_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    plan_id uuid NOT NULL,
    plan_member_id uuid NOT NULL,
    plan_config_id uuid NOT NULL,
    job_key char(64) NOT NULL,
    status text DEFAULT 'PENDING' NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    claimed_by text NULL,
    claimed_at timestamptz NULL,
    lease_expires_at timestamptz NULL,
    terminal_attempt_id uuid NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_experiment_jobs PRIMARY KEY (id),
    CONSTRAINT uq_jobs_job_key UNIQUE (job_key),
    CONSTRAINT uq_jobs_logical_identity UNIQUE (plan_id, plan_member_id, plan_config_id),
    CONSTRAINT uq_jobs_id_plan UNIQUE (id, plan_id),
    CONSTRAINT uq_jobs_id_job_key UNIQUE (id, job_key),
    CONSTRAINT ck_jobs_job_key_hex CHECK (job_key ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_jobs_status CHECK (status IN ('PENDING','CLAIMED','RUNNING','SUCCEEDED','FAILED')),
    CONSTRAINT ck_jobs_claim_invariant CHECK ((status = 'PENDING' AND claimed_by IS NULL AND claimed_at IS NULL) OR (status <> 'PENDING' AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL)),
    CONSTRAINT ck_jobs_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT fk_jobs_plan FOREIGN KEY (plan_id) REFERENCES dbv2_experiments.experiment_plans (id),
    CONSTRAINT fk_jobs_member_plan FOREIGN KEY (plan_member_id, plan_id) REFERENCES dbv2_experiments.experiment_plan_members (id, plan_id),
    CONSTRAINT fk_jobs_config_plan FOREIGN KEY (plan_config_id, plan_id) REFERENCES dbv2_experiments.experiment_plan_configs (id, plan_id)
);""",
    r"""CREATE INDEX ix_jobs_claim ON dbv2_experiments.experiment_jobs USING btree (plan_id, created_at, id) WHERE status = 'PENDING';""",
    r"""CREATE INDEX ix_jobs_plan_status ON dbv2_experiments.experiment_jobs USING btree (plan_id, status);""",
    r"""CREATE INDEX ix_jobs_worker_status ON dbv2_experiments.experiment_jobs USING btree (claimed_by, status) WHERE claimed_by IS NOT NULL;""",
    r"""CREATE INDEX ix_jobs_stale_leases ON dbv2_experiments.experiment_jobs USING btree (lease_expires_at) WHERE status IN ('CLAIMED','RUNNING');""",
    r"""CREATE TABLE dbv2_experiments.execution_attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    attempt_number integer NOT NULL,
    worker_id text NOT NULL,
    started_at timestamptz DEFAULT now() NOT NULL,
    finished_at timestamptz NULL,
    outcome text NULL,
    runtime_ms bigint NULL,
    gatk_executable_sha256 char(64) NULL,
    gatk_version text NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_execution_attempts PRIMARY KEY (id),
    CONSTRAINT uq_attempts_job_number UNIQUE (job_id, attempt_number),
    CONSTRAINT uq_attempts_id_job UNIQUE (id, job_id),
    CONSTRAINT ck_attempts_number CHECK (attempt_number >= 1),
    CONSTRAINT ck_attempts_outcome CHECK (outcome IS NULL OR outcome IN ('SUCCEEDED','FAILED','ABANDONED')),
    CONSTRAINT ck_attempts_runtime CHECK (runtime_ms IS NULL OR runtime_ms >= 0),
    CONSTRAINT ck_attempts_worker CHECK (length(worker_id) > 0),
    CONSTRAINT fk_attempts_job_plan FOREIGN KEY (job_id, plan_id) REFERENCES dbv2_experiments.experiment_jobs (id, plan_id)
);""",
    r"""CREATE INDEX ix_attempts_job ON dbv2_experiments.execution_attempts USING btree (job_id, attempt_number);""",
    r"""CREATE INDEX ix_attempts_open ON dbv2_experiments.execution_attempts USING btree (started_at) WHERE finished_at IS NULL;""",
    r"""CREATE TABLE dbv2_experiments.execution_failures (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attempt_id uuid NOT NULL,
    job_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    failure_code text NOT NULL,
    exit_code integer NULL,
    stderr_sha256 char(64) NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_execution_failures PRIMARY KEY (id),
    CONSTRAINT uq_failures_attempt UNIQUE (attempt_id),
    CONSTRAINT ck_failures_code_bounded CHECK (failure_code IN ('PREPARATION_FAILED','GATK_NONZERO_EXIT','GATK_TIMEOUT','GATK_OUTPUT_INVALID','GATK_OUTPUT_MISSING','EXECUTION_ERROR')),
    CONSTRAINT ck_failures_stderr_hex CHECK (stderr_sha256 IS NULL OR stderr_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_failures_attempt_job FOREIGN KEY (attempt_id, job_id) REFERENCES dbv2_experiments.execution_attempts (id, job_id),
    CONSTRAINT fk_failures_job_plan FOREIGN KEY (job_id, plan_id) REFERENCES dbv2_experiments.experiment_jobs (id, plan_id)
);""",
    r"""CREATE INDEX ix_failures_job ON dbv2_experiments.execution_failures USING btree (job_id);""",
    r"""CREATE INDEX ix_failures_code ON dbv2_experiments.execution_failures USING btree (failure_code);""",
    r"""CREATE TABLE dbv2_experiments.execution_results (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attempt_id uuid NOT NULL,
    job_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    job_key char(64) NOT NULL,
    result_hash char(64) NOT NULL,
    input_identity_hash char(64) NOT NULL,
    logical_argv_hash char(64) NOT NULL,
    vcf_artifact_id uuid NOT NULL,
    manifest_artifact_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_execution_results PRIMARY KEY (id),
    CONSTRAINT uq_results_attempt UNIQUE (attempt_id),
    CONSTRAINT uq_results_job UNIQUE (job_id),
    CONSTRAINT uq_results_result_hash UNIQUE (result_hash),
    CONSTRAINT ck_results_result_hex CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_results_input_hex CHECK (input_identity_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_results_argv_hex CHECK (logical_argv_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_results_distinct_artifacts CHECK (vcf_artifact_id <> manifest_artifact_id),
    CONSTRAINT fk_results_attempt_job FOREIGN KEY (attempt_id, job_id) REFERENCES dbv2_experiments.execution_attempts (id, job_id),
    CONSTRAINT fk_results_job_plan FOREIGN KEY (job_id, plan_id) REFERENCES dbv2_experiments.experiment_jobs (id, plan_id),
    CONSTRAINT fk_results_job_key FOREIGN KEY (job_id, job_key) REFERENCES dbv2_experiments.experiment_jobs (id, job_key),
    CONSTRAINT fk_results_vcf_artifact FOREIGN KEY (vcf_artifact_id) REFERENCES dbv2_catalog.artifacts (id),
    CONSTRAINT fk_results_manifest_artifact FOREIGN KEY (manifest_artifact_id) REFERENCES dbv2_catalog.artifacts (id)
);""",
    r"""CREATE INDEX ix_results_plan ON dbv2_experiments.execution_results USING btree (plan_id);""",
    r"""CREATE TABLE dbv2_evaluation.evaluation_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    execution_result_id uuid NOT NULL,
    truth_binding_id uuid NOT NULL,
    evaluator_version text NOT NULL,
    evaluation_hash char(64) NOT NULL,
    state text DEFAULT 'pending' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    completed_at timestamptz NULL,
    CONSTRAINT pk_evaluation_runs PRIMARY KEY (id),
    CONSTRAINT uq_evaluation_runs_result_version UNIQUE (execution_result_id, evaluator_version),
    CONSTRAINT uq_evaluation_runs_hash UNIQUE (evaluation_hash),
    CONSTRAINT ck_evaluation_runs_hash_hex CHECK (evaluation_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evaluation_runs_state CHECK (state IN ('pending','running','complete','failed')),
    CONSTRAINT fk_evaluation_runs_result FOREIGN KEY (execution_result_id) REFERENCES dbv2_experiments.execution_results (id),
    CONSTRAINT fk_evaluation_runs_truth FOREIGN KEY (truth_binding_id) REFERENCES dbv2_evaluation.truth_bindings (id)
);""",
    r"""CREATE INDEX ix_evaluation_runs_state ON dbv2_evaluation.evaluation_runs USING btree (state) WHERE state <> 'complete';""",
    r"""CREATE TABLE dbv2_evaluation.evaluation_metrics (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    evaluation_run_id uuid NOT NULL,
    metric_name text NOT NULL,
    metric_value double precision NOT NULL,
    variant_class text NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_evaluation_metrics PRIMARY KEY (id),
    CONSTRAINT uq_evaluation_metrics_named UNIQUE (evaluation_run_id, metric_name, variant_class),
    CONSTRAINT ck_evaluation_metrics_finite CHECK (metric_value = metric_value AND metric_value <> 'Infinity'::double precision AND metric_value <> '-Infinity'::double precision),
    CONSTRAINT fk_evaluation_metrics_run FOREIGN KEY (evaluation_run_id) REFERENCES dbv2_evaluation.evaluation_runs (id)
);""",
    r"""CREATE INDEX ix_evaluation_metrics_run ON dbv2_evaluation.evaluation_metrics USING btree (evaluation_run_id);""",
    r"""CREATE TABLE dbv2_evaluation.evaluation_scores (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    evaluation_run_id uuid NOT NULL,
    execution_result_id uuid NOT NULL,
    score double precision NOT NULL,
    scoring_version text NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_evaluation_scores PRIMARY KEY (id),
    CONSTRAINT uq_evaluation_scores_run UNIQUE (evaluation_run_id),
    CONSTRAINT ck_evaluation_scores_finite CHECK (score = score),
    CONSTRAINT fk_evaluation_scores_run FOREIGN KEY (evaluation_run_id) REFERENCES dbv2_evaluation.evaluation_runs (id),
    CONSTRAINT fk_evaluation_scores_result FOREIGN KEY (execution_result_id) REFERENCES dbv2_experiments.execution_results (id)
);""",
    r"""CREATE INDEX ix_evaluation_scores_ranking ON dbv2_evaluation.evaluation_scores USING btree (scoring_version, score);""",
    r"""CREATE TABLE dbv2_experiments.job_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    attempt_id uuid NULL,
    from_status text NULL,
    to_status text NOT NULL,
    actor_role text NOT NULL,
    worker_id text NULL,
    occurred_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_job_events PRIMARY KEY (id),
    CONSTRAINT ck_job_events_to_status CHECK (to_status IN ('PENDING','CLAIMED','RUNNING','SUCCEEDED','FAILED')),
    CONSTRAINT fk_job_events_job FOREIGN KEY (job_id) REFERENCES dbv2_experiments.experiment_jobs (id),
    CONSTRAINT fk_job_events_attempt FOREIGN KEY (attempt_id) REFERENCES dbv2_experiments.execution_attempts (id)
);""",
    r"""CREATE INDEX ix_job_events_job_time ON dbv2_experiments.job_events USING btree (job_id, occurred_at);""",
    r"""CREATE TABLE dbv2_runtime.active_selections (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    release_id uuid NOT NULL,
    model_version_id uuid NULL,
    candidate_config_id uuid NULL,
    effective_from timestamptz DEFAULT now() NOT NULL,
    effective_to timestamptz NULL,
    selected_by_role text NOT NULL,
    CONSTRAINT pk_active_selections PRIMARY KEY (id),
    CONSTRAINT ck_active_selections_window CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT fk_active_selections_release FOREIGN KEY (release_id) REFERENCES dbv2_catalog.releases (id),
    CONSTRAINT fk_active_selections_model FOREIGN KEY (model_version_id) REFERENCES dbv2_models.model_versions (id),
    CONSTRAINT fk_active_selections_config FOREIGN KEY (candidate_config_id) REFERENCES dbv2_experiments.candidate_configs (id)
);""",
    r"""CREATE UNIQUE INDEX uq_active_selections_single ON dbv2_runtime.active_selections USING btree (effective_to) WHERE effective_to IS NULL;""",
    r"""CREATE TABLE dbv2_runtime.leases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lease_key text NOT NULL,
    holder text NOT NULL,
    acquired_at timestamptz DEFAULT now() NOT NULL,
    expires_at timestamptz NOT NULL,
    fence_token bigint NOT NULL,
    CONSTRAINT pk_leases PRIMARY KEY (id),
    CONSTRAINT uq_leases_key UNIQUE (lease_key),
    CONSTRAINT ck_leases_window CHECK (expires_at > acquired_at),
    CONSTRAINT ck_leases_fence CHECK (fence_token >= 0)
);""",
    r"""CREATE INDEX ix_leases_expiry ON dbv2_runtime.leases USING btree (expires_at);""",
    r"""CREATE TABLE dbv2_runtime.service_instances (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_key text NOT NULL,
    service_name text NOT NULL,
    release_id uuid NULL,
    state text DEFAULT 'starting' NOT NULL,
    last_heartbeat_at timestamptz NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pk_service_instances PRIMARY KEY (id),
    CONSTRAINT uq_service_instances_key UNIQUE (instance_key),
    CONSTRAINT ck_service_instances_state CHECK (state IN ('starting','healthy','degraded','stopped')),
    CONSTRAINT fk_service_instances_release FOREIGN KEY (release_id) REFERENCES dbv2_catalog.releases (id)
);""",
    r"""CREATE INDEX ix_service_instances_heartbeat ON dbv2_runtime.service_instances USING btree (last_heartbeat_at) WHERE state <> 'stopped';""",
    r"""CREATE FUNCTION dbv2_audit.reject_delete()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    RAISE EXCEPTION 'DELETE on %.% is not permitted', TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_audit.reject_immutable_column_update()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    col text;
    before_row jsonb := to_jsonb(OLD);
    after_row jsonb := to_jsonb(NEW);
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF before_row -> col IS DISTINCT FROM after_row -> col THEN
            RAISE EXCEPTION 'immutable column %.%.% may not change',
                TG_TABLE_SCHEMA, TG_TABLE_NAME, col
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_audit.reject_update()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    RAISE EXCEPTION 'UPDATE on %.% is not permitted: every column is immutable',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
    RETURN NULL;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.enforce_artifact_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.lifecycle_state IS DISTINCT FROM OLD.lifecycle_state
       AND (coalesce(OLD.lifecycle_state::text, '(null)'), coalesce(NEW.lifecycle_state::text, '(null)')) NOT IN (('active', 'archived'), ('active', 'quarantined'), ('active', 'deleted'), ('archived', 'quarantined'), ('archived', 'deleted'), ('quarantined', 'deleted')) THEN
        RAISE EXCEPTION 'forbidden lifecycle transition % -> %',
            coalesce(OLD.lifecycle_state::text, '(null)'),
            coalesce(NEW.lifecycle_state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.verification_state IS DISTINCT FROM OLD.verification_state
       AND (coalesce(OLD.verification_state::text, '(null)'), coalesce(NEW.verification_state::text, '(null)')) NOT IN (('unverified', 'verified'), ('unverified', 'missing'), ('unverified', 'corrupt'), ('verified', 'missing'), ('verified', 'corrupt'), ('missing', 'verified'), ('missing', 'corrupt'), ('corrupt', 'verified'), ('corrupt', 'missing')) THEN
        RAISE EXCEPTION 'forbidden verification transition % -> %',
            coalesce(OLD.verification_state::text, '(null)'),
            coalesce(NEW.verification_state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.first_verified_at IS NOT NULL
       AND NEW.first_verified_at IS DISTINCT FROM OLD.first_verified_at THEN
        RAISE EXCEPTION 'first_verified_at is written exactly once'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.verification_state = 'verified' AND OLD.verification_state <> 'verified'
       AND NEW.first_verified_at IS NULL THEN
        RAISE EXCEPTION 'the first transition to verified must set first_verified_at'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.last_verified_at IS NOT NULL AND NEW.last_verified_at IS NOT NULL
       AND NEW.last_verified_at < OLD.last_verified_at THEN
        RAISE EXCEPTION 'last_verified_at may not move backwards'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.enforce_artifact_location_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    other_primary uuid;
BEGIN
    IF TG_OP = 'UPDATE' THEN
    IF NEW.location_state IS DISTINCT FROM OLD.location_state
       AND (coalesce(OLD.location_state::text, '(null)'), coalesce(NEW.location_state::text, '(null)')) NOT IN (('present', 'missing'), ('present', 'corrupt'), ('present', 'evacuated'), ('missing', 'present'), ('missing', 'corrupt'), ('missing', 'evacuated'), ('corrupt', 'present'), ('corrupt', 'missing'), ('corrupt', 'evacuated')) THEN
        RAISE EXCEPTION 'forbidden location transition % -> %',
            coalesce(OLD.location_state::text, '(null)'),
            coalesce(NEW.location_state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    END IF;
    IF NEW.is_primary AND NEW.location_state <> 'present' THEN
        RAISE EXCEPTION 'only a present location may be primary (state %)',
            NEW.location_state USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.is_primary THEN
        PERFORM 1 FROM dbv2_catalog.artifacts
            WHERE id = NEW.artifact_id FOR UPDATE;
        SELECT id INTO other_primary FROM dbv2_catalog.artifact_locations
            WHERE artifact_id = NEW.artifact_id AND is_primary AND id <> NEW.id
            LIMIT 1;
        IF other_primary IS NOT NULL THEN
            RAISE EXCEPTION 'artifact % already has a primary location', NEW.artifact_id
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.enforce_backup_set_immutability()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    col text;
    before_row jsonb := to_jsonb(OLD);
    after_row jsonb := to_jsonb(NEW);
BEGIN
    FOREACH col IN ARRAY ARRAY(SELECT jsonb_object_keys(before_row) ORDER BY 1) LOOP
        IF col <> 'restore_tested_at'
           AND before_row -> col IS DISTINCT FROM after_row -> col THEN
            RAISE EXCEPTION 'backup_sets.% is immutable', col
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    IF OLD.restore_tested_at IS NOT NULL AND NEW.restore_tested_at IS NOT NULL
       AND NEW.restore_tested_at < OLD.restore_tested_at THEN
        RAISE EXCEPTION 'restore_tested_at may not move backwards'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.enforce_backup_set_shape()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    binding record;
    art record;
    r1 jsonb;
    snap jsonb;
    entry_count bigint;
    entry_bytes bigint;
    db_count bigint;
    db_bytes bigint;
    offenders bigint;
    conflicting uuid;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.completeness IS DISTINCT FROM OLD.completeness THEN
            RAISE EXCEPTION 'completeness changed: % -> %',
                OLD.completeness, NEW.completeness USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END IF;
    SELECT id INTO conflicting FROM dbv2_catalog.backup_sets
        WHERE id <> NEW.id
          AND (recovery_set_id = NEW.recovery_set_id OR backup_key = NEW.backup_key
               OR recovery_manifest_sha256 = NEW.recovery_manifest_sha256);
    IF conflicting IS NOT NULL THEN
        RAISE EXCEPTION 'conflicting recovery_set_id, backup_key or recovery_manifest_sha256 (row %)', conflicting USING ERRCODE = 'unique_violation';
    END IF;
    FOR binding IN
        SELECT * FROM (VALUES
            ('recovery manifest', NEW.recovery_manifest_artifact_id,
             NEW.recovery_manifest_sha256, NEW.recovery_manifest_media_type, 'inline',
             'minos-db-recovery-manifest-v1'),
            ('database backup', NEW.database_backup_artifact_id,
             NEW.database_backup_sha256, NEW.database_backup_media_type, 'external',
             NULL),
            ('artifact snapshot manifest', NEW.artifact_snapshot_manifest_artifact_id,
             NEW.artifact_snapshot_manifest_sha256,
             NEW.artifact_snapshot_manifest_media_type, 'inline', 'minos-artifact-snapshot-v1')
        ) AS v(label, artifact_id, digest, media_type, storage, schema_version)
        WHERE v.artifact_id IS NOT NULL
    LOOP
        SELECT * INTO art FROM dbv2_catalog.artifacts
            WHERE id = binding.artifact_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'a referenced artifact does not exist: % (%)',
                binding.artifact_id, binding.label
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF art.content_sha256 <> binding.digest
           OR art.media_type <> binding.media_type THEN
            RAISE EXCEPTION '% triple does not bind one artifact', binding.label
                USING ERRCODE = 'check_violation';
        END IF;
        IF art.verification_state <> 'verified' THEN
            RAISE EXCEPTION '% is not verification_state = verified (%)',
                binding.label, art.verification_state
                USING ERRCODE = 'check_violation';
        END IF;
        IF art.lifecycle_state <> 'active' THEN
            RAISE EXCEPTION '% is not lifecycle_state = active (%)',
                binding.label, art.lifecycle_state
                USING ERRCODE = 'check_violation';
        END IF;
        IF art.backup_scope <> 'recovery' THEN
            RAISE EXCEPTION '% is not backup_scope = recovery (%)',
                binding.label, art.backup_scope USING ERRCODE = 'check_violation';
        END IF;
        IF art.storage_mode <> binding.storage THEN
            RAISE EXCEPTION '% is not stored in its declared storage mode: % (expected %)', binding.label, art.storage_mode, binding.storage
                USING ERRCODE = 'check_violation';
        END IF;
        IF binding.schema_version IS NOT NULL
           AND art.schema_version IS DISTINCT FROM binding.schema_version THEN
            RAISE EXCEPTION '% declares schema_version %, not %',
                binding.label, art.schema_version, binding.schema_version
                USING ERRCODE = 'check_violation';
        END IF;
        IF binding.storage = 'inline' THEN
            IF encode(sha256(art.inline_payload), 'hex') <> binding.digest THEN
                RAISE EXCEPTION 'inline manifest bytes do not recompute to their raw digest (%)', binding.label USING ERRCODE = 'check_violation';
            END IF;
            IF octet_length(art.inline_payload) <> art.size_bytes THEN
                RAISE EXCEPTION 'inline manifest byte size does not match the stored payload (%)', binding.label USING ERRCODE = 'check_violation';
            END IF;
        ELSE
            PERFORM 1 FROM dbv2_catalog.artifact_locations
                WHERE artifact_id = art.id AND location_state = 'present' LIMIT 1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'the external database dump has no artifact_locations row in state present'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    END LOOP;
    SELECT * INTO art FROM dbv2_catalog.artifacts
        WHERE id = NEW.recovery_manifest_artifact_id;
    IF encode(sha256(art.inline_payload), 'hex') <> NEW.recovery_manifest_sha256 THEN
        RAISE EXCEPTION 'recovery manifest bytes do not recompute to recovery_manifest_sha256' USING ERRCODE = 'check_violation';
    END IF;
    r1 := convert_from(art.inline_payload, 'UTF8')::jsonb;
    IF (r1 ->> 'artifact_count')::bigint IS DISTINCT FROM NEW.artifact_count THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'artifact_count', 'artifact_count' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'artifact_snapshot_manifest_sha256') IS DISTINCT FROM NEW.artifact_snapshot_manifest_sha256 THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'artifact_snapshot_manifest_sha256', 'artifact_snapshot_manifest_sha256' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'artifact_snapshot_sha256') IS DISTINCT FROM NEW.artifact_snapshot_sha256 THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'artifact_snapshot_sha256', 'artifact_snapshot_sha256' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'artifact_total_bytes')::bigint IS DISTINCT FROM NEW.artifact_total_bytes THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'artifact_total_bytes', 'artifact_total_bytes' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'artifact_verification_tool_version') IS DISTINCT FROM NEW.artifact_verification_tool_version THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'artifact_verification_tool_version', 'artifact_verification_tool_version' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'backup_tool_version') IS DISTINCT FROM NEW.backup_tool_version THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'backup_tool_version', 'backup_tool_version' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'created_at')::timestamptz IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'created_at', 'created_at' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'database_backup_kind') IS DISTINCT FROM NEW.database_backup_kind THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'database_backup_kind', 'database_backup_kind' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'database_backup_sha256') IS DISTINCT FROM NEW.database_backup_sha256 THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'database_backup_sha256', 'database_backup_sha256' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'database_backup_size_bytes')::bigint IS DISTINCT FROM NEW.database_backup_size_bytes THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'database_backup_size_bytes', 'database_backup_size_bytes' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'database_name') IS DISTINCT FROM NEW.database_name THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'database_name', 'database_name' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'postgresql_version') IS DISTINCT FROM NEW.postgresql_version THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'postgresql_version', 'postgresql_version' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'quiesce_ended_at')::timestamptz IS DISTINCT FROM NEW.quiesce_ended_at THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'quiesce_ended_at', 'quiesce_ended_at' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'quiesce_started_at')::timestamptz IS DISTINCT FROM NEW.quiesce_started_at THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'quiesce_started_at', 'quiesce_started_at' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'recovery_set_id')::uuid IS DISTINCT FROM NEW.recovery_set_id THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'recovery_set_id', 'recovery_set_id' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'schema_version') IS DISTINCT FROM NEW.manifest_schema_version THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'schema_version', 'manifest_schema_version' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'source_alembic_revision') IS DISTINCT FROM NEW.alembic_revision THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'source_alembic_revision', 'alembic_revision' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'wal_end_lsn') IS DISTINCT FROM NEW.wal_end_lsn THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'wal_end_lsn', 'wal_end_lsn' USING ERRCODE = 'check_violation';
    END IF;
    IF (r1 ->> 'wal_start_lsn') IS DISTINCT FROM NEW.wal_start_lsn THEN
        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',
            'wal_start_lsn', 'wal_start_lsn' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.artifact_snapshot_manifest_artifact_id IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT * INTO art FROM dbv2_catalog.artifacts
        WHERE id = NEW.artifact_snapshot_manifest_artifact_id;
    IF encode(sha256(convert_to(E'minos:db-v2-artifact-snapshot:v1\n', 'UTF8') || art.inline_payload),
              'hex') <> NEW.artifact_snapshot_sha256 THEN
        RAISE EXCEPTION 'snapshot manifest bytes do not recompute to artifact_snapshot_sha256' USING ERRCODE = 'check_violation';
    END IF;
    snap := convert_from(art.inline_payload, 'UTF8')::jsonb;
    IF snap ->> 'predicate' IS DISTINCT FROM 'lifecycle_state = ''active'' AND backup_scope = ''operational''' THEN
        RAISE EXCEPTION 'the snapshot was not taken with the frozen predicate'
            USING ERRCODE = 'check_violation';
    END IF;
    IF snap ->> 'schema_version' IS DISTINCT FROM 'minos-artifact-snapshot-v1' THEN
        RAISE EXCEPTION 'the snapshot declares schema_version %, not %',
            snap ->> 'schema_version', 'minos-artifact-snapshot-v1'
            USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_typeof(snap -> 'entries') <> 'array' THEN
        RAISE EXCEPTION 'the snapshot entries are not a JSON array'
            USING ERRCODE = 'check_violation';
    END IF;
    -- 1/2: exactly the three canonical fields, each of the right type and shape
    SELECT count(*) INTO offenders FROM jsonb_array_elements(snap -> 'entries') AS e
        WHERE (SELECT count(*) FROM jsonb_object_keys(e)) <> 3
           OR NOT (e ? 'content_sha256' AND e ? 'size_bytes' AND e ? 'artifact_kind')
           OR jsonb_typeof(e -> 'content_sha256') <> 'string'
           OR jsonb_typeof(e -> 'size_bytes') <> 'number'
           OR jsonb_typeof(e -> 'artifact_kind') <> 'string'
           OR (e ->> 'content_sha256') !~ '^[0-9a-f]{64}$'
           OR (e ->> 'size_bytes')::numeric < 0
           OR (e ->> 'size_bytes')::numeric <> trunc((e ->> 'size_bytes')::numeric)
           OR length(e ->> 'artifact_kind') = 0;
    IF offenders <> 0 THEN
        RAISE EXCEPTION '% snapshot entries have a noncanonical field inventory, type or value', offenders USING ERRCODE = 'check_violation';
    END IF;
    -- 3: unique by the complete triple
    SELECT count(*) INTO offenders FROM (
        SELECT e ->> 'content_sha256' AS content_sha256,
               (e ->> 'size_bytes')::bigint AS size_bytes,
               e ->> 'artifact_kind' AS artifact_kind
        FROM jsonb_array_elements(snap -> 'entries') AS e
        GROUP BY 1, 2, 3 HAVING count(*) > 1
    ) AS duplicated;
    IF offenders <> 0 THEN
        RAISE EXCEPTION 'the snapshot repeats % entries', offenders
            USING ERRCODE = 'check_violation';
    END IF;
    -- 4: deterministic ascending order
    SELECT count(*) INTO offenders FROM (
        SELECT position,
               row_number() OVER (ORDER BY e ->> 'content_sha256', (e ->> 'size_bytes')::bigint, e ->> 'artifact_kind') AS expected
        FROM jsonb_array_elements(snap -> 'entries') WITH ORDINALITY AS t(e, position)
    ) AS ordered WHERE position <> expected;
    IF offenders <> 0 THEN
        RAISE EXCEPTION 'the snapshot entries are not in the frozen ascending order'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*), coalesce(sum((e ->> 'size_bytes')::bigint), 0)
        INTO entry_count, entry_bytes
        FROM jsonb_array_elements(snap -> 'entries') AS e;
    SELECT count(*), coalesce(sum(size_bytes), 0) INTO db_count, db_bytes
        FROM dbv2_catalog.artifacts WHERE lifecycle_state = 'active' AND backup_scope = 'operational';
    -- the artifact-catalog bootstrap (B0) must have run: 0009 creates the catalog EMPTY
    IF db_count = 0 AND entry_count > 0 THEN
        RAISE EXCEPTION 'the artifact-catalog bootstrap (B0) has not run: the shadow artifact catalog holds no active operational artifact, so a complete snapshot cannot be registered' USING ERRCODE = 'check_violation';
    END IF;
    -- 5: exact bidirectional set equality, multiplicity included
    SELECT count(*) INTO offenders FROM (
        SELECT e ->> 'content_sha256' AS content_sha256,
               (e ->> 'size_bytes')::bigint AS size_bytes,
               e ->> 'artifact_kind' AS artifact_kind
        FROM jsonb_array_elements(snap -> 'entries') AS e
        EXCEPT ALL
        SELECT content_sha256, size_bytes, artifact_kind
        FROM dbv2_catalog.artifacts WHERE lifecycle_state = 'active' AND backup_scope = 'operational'
    ) AS extra;
    IF offenders <> 0 THEN
        RAISE EXCEPTION '% snapshot entries do not resolve to an active operational artifact', offenders USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO offenders FROM (
        SELECT content_sha256, size_bytes, artifact_kind
        FROM dbv2_catalog.artifacts WHERE lifecycle_state = 'active' AND backup_scope = 'operational'
        EXCEPT ALL
        SELECT e ->> 'content_sha256' AS content_sha256,
               (e ->> 'size_bytes')::bigint AS size_bytes,
               e ->> 'artifact_kind' AS artifact_kind
        FROM jsonb_array_elements(snap -> 'entries') AS e
    ) AS omitted;
    IF offenders <> 0 THEN
        RAISE EXCEPTION '% active operational artifacts are absent from the snapshot',
            offenders USING ERRCODE = 'check_violation';
    END IF;
    -- 6/7: every count and every total agrees, four ways and three ways
    IF entry_count IS DISTINCT FROM NEW.artifact_count
       OR (snap ->> 'artifact_count')::bigint IS DISTINCT FROM NEW.artifact_count
       OR db_count IS DISTINCT FROM NEW.artifact_count THEN
        RAISE EXCEPTION 'snapshot entry count <> artifact_count (json %, database %, row %)', entry_count, db_count, NEW.artifact_count
            USING ERRCODE = 'check_violation';
    END IF;
    IF entry_bytes IS DISTINCT FROM NEW.artifact_total_bytes
       OR (snap ->> 'artifact_total_bytes')::bigint
          IS DISTINCT FROM NEW.artifact_total_bytes
       OR db_bytes IS DISTINCT FROM NEW.artifact_total_bytes THEN
        RAISE EXCEPTION 'snapshot entry total size <> artifact_total_bytes (json %, database %, row %)', entry_bytes, db_bytes, NEW.artifact_total_bytes
            USING ERRCODE = 'check_violation';
    END IF;
    -- 8: every included EXTERNAL artifact is verified, present, and singly primary
    SELECT count(*) INTO offenders
        FROM dbv2_catalog.artifacts AS a
        WHERE a.lifecycle_state = 'active' AND a.backup_scope = 'operational'
          AND a.storage_mode = 'external'
          AND (a.verification_state <> 'verified'
               OR NOT EXISTS (SELECT 1 FROM dbv2_catalog.artifact_locations AS l
                              WHERE l.artifact_id = a.id
                                AND l.location_state = 'present')
               OR (SELECT count(*) FROM dbv2_catalog.artifact_locations AS l
                   WHERE l.artifact_id = a.id AND l.location_state = 'present'
                     AND l.is_primary) <> 1);
    IF offenders <> 0 THEN
        RAISE EXCEPTION '% snapshotted external artifacts are unverified, absent or ambiguously primary', offenders USING ERRCODE = 'check_violation';
    END IF;
    -- 9: every included INLINE artifact still recomputes from its own bytes
    SELECT count(*) INTO offenders
        FROM dbv2_catalog.artifacts AS a
        WHERE a.lifecycle_state = 'active' AND a.backup_scope = 'operational'
          AND a.storage_mode = 'inline'
          AND (encode(sha256(a.inline_payload), 'hex') <> a.content_sha256
               OR octet_length(a.inline_payload) <> a.size_bytes);
    IF offenders <> 0 THEN
        RAISE EXCEPTION '% snapshotted inline artifacts do not recompute', offenders
            USING ERRCODE = 'check_violation';
    END IF;
    -- 10: no recovery-scope artifact on either side
    SELECT count(*) INTO offenders
        FROM jsonb_array_elements(snap -> 'entries') AS e
        JOIN dbv2_catalog.artifacts AS a
          ON a.content_sha256 = e ->> 'content_sha256'
        WHERE a.backup_scope = 'recovery';
    IF offenders <> 0 THEN
        RAISE EXCEPTION '% recovery artifacts appear in the snapshot', offenders
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.enforce_release_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    other_active uuid;
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state
       AND (coalesce(OLD.state::text, '(null)'), coalesce(NEW.state::text, '(null)')) NOT IN (('draft', 'qualified'), ('draft', 'superseded'), ('qualified', 'active'), ('qualified', 'superseded'), ('active', 'superseded')) THEN
        RAISE EXCEPTION 'forbidden release transition % -> %',
            coalesce(OLD.state::text, '(null)'),
            coalesce(NEW.state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state = 'active' AND OLD.state <> 'active' THEN
        SELECT id INTO other_active FROM dbv2_catalog.releases
            WHERE state = 'active' AND id <> NEW.id FOR UPDATE;
        IF other_active IS NOT NULL THEN
            RAISE EXCEPTION 'release % is already active', other_active
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    backend record;
    existing record;
    audited_id uuid;
BEGIN
    IF p_object_key IS NULL OR length(p_object_key) = 0 THEN
        RAISE EXCEPTION 'object_key must be a non-empty relative key'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_object_key ~ '^/' OR p_object_key ~ '^[a-zA-Z][a-zA-Z0-9+.-]*://'
       OR p_object_key ~ '(^|/)[.][.](/|$)' OR p_object_key ~ '//'
       OR p_object_key ~ '/$' OR p_object_key ~ '^[.]/' THEN
        RAISE EXCEPTION 'object_key % is not a clean relative key', p_object_key
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM 1 FROM dbv2_catalog.artifacts WHERE id = p_artifact_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown artifact %', p_artifact_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    SELECT * INTO backend FROM dbv2_catalog.storage_backends
        WHERE backend_key = p_backend_key;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown storage backend %', p_backend_key
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    BEGIN
        INSERT INTO dbv2_catalog.artifact_locations
            (artifact_id, backend_id, object_key, location_state, is_primary)
        VALUES (p_artifact_id, backend.id, p_object_key, 'present', p_is_primary)
        RETURNING id INTO audited_id;
    EXCEPTION WHEN unique_violation THEN
        audited_id := NULL;
    END;
    IF audited_id IS NULL THEN
        SELECT * INTO existing FROM dbv2_catalog.artifact_locations
            WHERE (backend_id = backend.id AND object_key = p_object_key)
               OR (artifact_id = p_artifact_id AND backend_id = backend.id)
            FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'a uniqueness conflict on %/% resolved to no row',
                p_backend_key, p_object_key USING ERRCODE = 'unique_violation';
        END IF;
        IF existing.artifact_id IS DISTINCT FROM p_artifact_id
           OR existing.backend_id IS DISTINCT FROM backend.id
           OR existing.object_key IS DISTINCT FROM p_object_key
           OR existing.is_primary IS DISTINCT FROM p_is_primary THEN
            RAISE EXCEPTION 'location %/% is already registered with a different identity', p_backend_key, existing.object_key USING ERRCODE = 'unique_violation';
        END IF;
        RETURN existing.id;
    END IF;
    INSERT INTO dbv2_audit.events
        (actor_role, action, object_schema, object_table, object_id, payload_hash)
    VALUES (session_user, 'artifact_location.registered', 'dbv2_catalog', 'artifact_locations',
            audited_id, encode(sha256(convert_to((jsonb_build_object('action', 'artifact_location.registered',
                'artifact_id', p_artifact_id, 'backend_key', p_backend_key,
                'object_key', p_object_key, 'is_primary', p_is_primary,
                'location_state', 'present'))::text, 'UTF8')), 'hex'));
    RETURN audited_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    existing record;
    audited_id uuid;
    digest text;
    payload_size bigint;
BEGIN
    IF p_content_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'content_sha256 must be 64 lowercase hex characters'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_size_bytes IS NULL OR p_size_bytes < 0 THEN
        RAISE EXCEPTION 'size_bytes must be non-negative, got %', p_size_bytes
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_backup_scope NOT IN ('operational', 'recovery') THEN
        RAISE EXCEPTION 'backup_scope must be operational or recovery, got %',
            p_backup_scope USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_backup_scope = 'recovery' AND session_user IN ('minos_planner', 'minos_enqueue', 'minos_runner', 'minos_verifier', 'minos_trainer', 'minos_evaluator', 'minos_live') THEN
        RAISE EXCEPTION 'role % may not create a recovery-scope artifact', session_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    digest := p_content_sha256;
    payload_size := p_size_bytes;
    BEGIN
        INSERT INTO dbv2_catalog.artifacts
            (artifact_kind, content_sha256, size_bytes, media_type, storage_mode,
             lifecycle_state, retention_class, backup_scope, schema_version, provenance,
             verification_state)
        VALUES (p_artifact_kind, digest, payload_size, p_media_type, 'external',
                'active', p_retention_class, p_backup_scope, p_schema_version,
                p_provenance, 'unverified')
        RETURNING id INTO audited_id;
    EXCEPTION WHEN unique_violation THEN
        audited_id := NULL;
    END;
    IF audited_id IS NULL THEN
        SELECT * INTO existing FROM dbv2_catalog.artifacts
            WHERE content_sha256 = digest FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'a uniqueness conflict on % resolved to no row', digest
                USING ERRCODE = 'unique_violation';
        END IF;
        IF existing.content_sha256 IS DISTINCT FROM digest
           OR existing.size_bytes IS DISTINCT FROM payload_size
           OR existing.media_type IS DISTINCT FROM p_media_type
           OR existing.artifact_kind IS DISTINCT FROM p_artifact_kind
           OR existing.storage_mode IS DISTINCT FROM 'external'
           OR existing.retention_class IS DISTINCT FROM p_retention_class
           OR existing.backup_scope IS DISTINCT FROM p_backup_scope
           OR existing.schema_version IS DISTINCT FROM p_schema_version
           OR existing.provenance IS DISTINCT FROM p_provenance THEN
            RAISE EXCEPTION 'artifact % already exists with different immutable metadata', existing.content_sha256 USING ERRCODE = 'unique_violation';
        END IF;
        RETURN existing.id;
    END IF;
    INSERT INTO dbv2_audit.events
        (actor_role, action, object_schema, object_table, object_id, payload_hash)
    VALUES (session_user, 'artifact.registered_external', 'dbv2_catalog', 'artifacts',
            audited_id, encode(sha256(convert_to((jsonb_build_object('action', 'artifact.registered_external',
                'content_sha256', digest, 'size_bytes', payload_size,
                'media_type', p_media_type, 'artifact_kind', p_artifact_kind,
                'storage_mode', 'external', 'retention_class', p_retention_class,
                'backup_scope', p_backup_scope, 'schema_version', p_schema_version,
                'provenance', p_provenance))::text, 'UTF8')), 'hex'));
    RETURN audited_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    existing record;
    audited_id uuid;
    digest text;
    payload_size bigint;
BEGIN
    IF p_payload IS NULL THEN
        RAISE EXCEPTION 'inline payload must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF octet_length(p_payload) > 65536 THEN
        RAISE EXCEPTION 'inline payload of % bytes exceeds the 65536-byte bound',
            octet_length(p_payload) USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_backup_scope NOT IN ('operational', 'recovery') THEN
        RAISE EXCEPTION 'backup_scope must be operational or recovery, got %',
            p_backup_scope USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_backup_scope = 'recovery' AND session_user IN ('minos_planner', 'minos_enqueue', 'minos_runner', 'minos_verifier', 'minos_trainer', 'minos_evaluator', 'minos_live') THEN
        RAISE EXCEPTION 'role % may not create a recovery-scope artifact', session_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    digest := encode(sha256(p_payload), 'hex');
    payload_size := octet_length(p_payload);
    -- insert first; a concurrent winner is resolved by re-reading, never by overwriting
    BEGIN
        INSERT INTO dbv2_catalog.artifacts
            (artifact_kind, content_sha256, size_bytes, media_type, storage_mode,
             inline_payload, lifecycle_state, retention_class, backup_scope,
             schema_version, provenance, verification_state, first_verified_at,
             last_verified_at)
        VALUES (p_artifact_kind, digest, payload_size, p_media_type, 'inline', p_payload,
                'active', p_retention_class, p_backup_scope, p_schema_version,
                p_provenance, 'verified', now(), now())
        RETURNING id INTO audited_id;
    EXCEPTION WHEN unique_violation THEN
        audited_id := NULL;
    END;
    IF audited_id IS NULL THEN
        SELECT * INTO existing FROM dbv2_catalog.artifacts
            WHERE content_sha256 = digest FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'a uniqueness conflict on % resolved to no row', digest
                USING ERRCODE = 'unique_violation';
        END IF;
        IF existing.content_sha256 IS DISTINCT FROM digest
           OR existing.size_bytes IS DISTINCT FROM payload_size
           OR existing.media_type IS DISTINCT FROM p_media_type
           OR existing.artifact_kind IS DISTINCT FROM p_artifact_kind
           OR existing.storage_mode IS DISTINCT FROM 'inline'
           OR existing.retention_class IS DISTINCT FROM p_retention_class
           OR existing.backup_scope IS DISTINCT FROM p_backup_scope
           OR existing.schema_version IS DISTINCT FROM p_schema_version
           OR existing.provenance IS DISTINCT FROM p_provenance
           OR existing.inline_payload IS DISTINCT FROM p_payload THEN
            RAISE EXCEPTION 'artifact % already exists with different immutable metadata', existing.content_sha256 USING ERRCODE = 'unique_violation';
        END IF;
        RETURN existing.id;
    END IF;
    INSERT INTO dbv2_audit.events
        (actor_role, action, object_schema, object_table, object_id, payload_hash)
    VALUES (session_user, 'artifact.published_inline', 'dbv2_catalog', 'artifacts',
            audited_id, encode(sha256(convert_to((jsonb_build_object('action', 'artifact.published_inline', 'content_sha256', digest,
                'size_bytes', payload_size, 'media_type', p_media_type,
                'artifact_kind', p_artifact_kind, 'storage_mode', 'inline',
                'retention_class', p_retention_class, 'backup_scope', p_backup_scope,
                'schema_version', p_schema_version, 'provenance', p_provenance))::text, 'UTF8')), 'hex'));
    RETURN audited_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid)
RETURNS text
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    art record;
    loc record;
    loc_id uuid;
    candidates bigint;
    audited_id uuid;
    outcome text;
    observed_digest text;
    observed_size bigint;
BEGIN
    SELECT * INTO art FROM dbv2_catalog.artifacts
        WHERE id = p_artifact_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown artifact %', p_artifact_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF art.storage_mode = 'inline' THEN
        -- the authoritative bytes are held here; a caller's claim about them is ignored
        observed_digest := encode(sha256(art.inline_payload), 'hex');
        observed_size := octet_length(art.inline_payload);
    ELSE
        observed_digest := p_observed_sha256;
        observed_size := p_observed_size_bytes;
        IF p_location_id IS NULL THEN
            SELECT count(*) INTO candidates FROM dbv2_catalog.artifact_locations
                WHERE artifact_id = art.id;
            IF candidates <> 1 THEN
                RAISE EXCEPTION 'an external artifact needs exactly one named location (% candidates)', candidates USING ERRCODE = 'invalid_parameter_value';
            END IF;
            SELECT * INTO loc FROM dbv2_catalog.artifact_locations
                WHERE artifact_id = art.id FOR UPDATE;
            loc_id := loc.id;
        ELSE
            SELECT * INTO loc FROM dbv2_catalog.artifact_locations
                WHERE id = p_location_id AND artifact_id = art.id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'location % does not belong to artifact %',
                    p_location_id, art.id USING ERRCODE = 'foreign_key_violation';
            END IF;
            loc_id := loc.id;
        END IF;
    END IF;
    IF observed_digest IS NULL OR observed_size IS NULL THEN
        outcome := 'missing';
    ELSIF observed_digest = art.content_sha256 AND observed_size = art.size_bytes THEN
        outcome := 'verified';
    ELSE
        outcome := 'corrupt';
    END IF;
    IF art.verification_state = outcome THEN
        -- a non-state-changing observation: refresh the timestamps, write no event
        UPDATE dbv2_catalog.artifacts SET last_verified_at = now()
            WHERE id = art.id;
        IF loc_id IS NOT NULL THEN
            UPDATE dbv2_catalog.artifact_locations SET last_verified_at = now()
                WHERE id = loc_id;
        END IF;
        RETURN outcome;
    END IF;
    UPDATE dbv2_catalog.artifacts
        SET verification_state = outcome,
            first_verified_at = CASE WHEN outcome = 'verified'
                                     THEN coalesce(art.first_verified_at, now())
                                     ELSE art.first_verified_at END,
            last_verified_at = now()
        WHERE id = art.id;
    IF loc_id IS NOT NULL THEN
        UPDATE dbv2_catalog.artifact_locations
            SET location_state = CASE WHEN outcome = 'verified' THEN 'present'
                                      WHEN outcome = 'missing' THEN 'missing'
                                      ELSE 'corrupt' END,
                -- a non-present location may not be primary; a restored one reclaims
                -- the role whenever no other present location already holds it
                is_primary = (outcome = 'verified' AND NOT EXISTS (
                    SELECT 1 FROM dbv2_catalog.artifact_locations AS other
                    WHERE other.artifact_id = art.id AND other.id <> loc_id
                      AND other.is_primary AND other.location_state = 'present')),
                last_verified_at = now()
            WHERE id = loc_id;
    END IF;
    audited_id := art.id;
    INSERT INTO dbv2_audit.events
        (actor_role, action, object_schema, object_table, object_id, payload_hash)
    VALUES (session_user, 'artifact.verification_recorded', 'dbv2_catalog', 'artifacts',
            audited_id, encode(sha256(convert_to((jsonb_build_object('action', 'artifact.verification_recorded',
                'artifact_id', art.id, 'content_sha256', art.content_sha256,
                'size_bytes', art.size_bytes, 'storage_mode', art.storage_mode,
                'from_state', art.verification_state, 'to_state', outcome,
                'location_id', loc_id))::text, 'UTF8')), 'hex'));
    RETURN outcome;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_catalog.register_backup_set(p_manifest jsonb, p_completeness text)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    existing record;
    new_id uuid;
BEGIN
    IF p_completeness NOT IN ('complete', 'database_only') THEN
        RAISE EXCEPTION 'completeness must be complete or database_only, got %',
            p_completeness USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- deterministic in the recovery set's own identity, released by commit or abort
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_manifest ->> 'recovery_set_id', 0));
    SELECT * INTO existing FROM dbv2_catalog.backup_sets
        WHERE recovery_set_id = (p_manifest ->> 'recovery_set_id')::uuid FOR UPDATE;
    IF FOUND THEN
        IF existing.backup_key IS DISTINCT FROM p_manifest ->> 'backup_key'
           OR existing.recovery_set_id IS DISTINCT FROM (p_manifest ->> 'recovery_set_id')::uuid
           OR existing.alembic_revision IS DISTINCT FROM p_manifest ->> 'source_alembic_revision'
           OR existing.quiesce_started_at IS DISTINCT FROM (p_manifest ->> 'quiesce_started_at')::timestamptz
           OR existing.quiesce_ended_at IS DISTINCT FROM (p_manifest ->> 'quiesce_ended_at')::timestamptz
           OR existing.manifest_schema_version IS DISTINCT FROM p_manifest ->> 'schema_version'
           OR existing.database_name IS DISTINCT FROM p_manifest ->> 'database_name'
           OR existing.recovery_manifest_artifact_id IS DISTINCT FROM (p_manifest ->> 'recovery_manifest_artifact_id')::uuid
           OR existing.recovery_manifest_sha256 IS DISTINCT FROM p_manifest ->> 'recovery_manifest_sha256'
           OR existing.database_backup_kind IS DISTINCT FROM p_manifest ->> 'database_backup_kind'
           OR existing.database_backup_artifact_id IS DISTINCT FROM (p_manifest ->> 'database_backup_artifact_id')::uuid
           OR existing.database_backup_sha256 IS DISTINCT FROM p_manifest ->> 'database_backup_sha256'
           OR existing.database_backup_size_bytes IS DISTINCT FROM (p_manifest ->> 'database_backup_size_bytes')::bigint
           OR existing.wal_start_lsn IS DISTINCT FROM p_manifest ->> 'wal_start_lsn'
           OR existing.wal_end_lsn IS DISTINCT FROM p_manifest ->> 'wal_end_lsn'
           OR existing.artifact_snapshot_manifest_artifact_id IS DISTINCT FROM (p_manifest ->> 'artifact_snapshot_manifest_artifact_id')::uuid
           OR existing.artifact_snapshot_manifest_sha256 IS DISTINCT FROM p_manifest ->> 'artifact_snapshot_manifest_sha256'
           OR existing.artifact_snapshot_manifest_media_type IS DISTINCT FROM (CASE WHEN p_completeness = 'complete' THEN 'application/vnd.minos.artifact-snapshot+json' END)
           OR existing.artifact_snapshot_sha256 IS DISTINCT FROM p_manifest ->> 'artifact_snapshot_sha256'
           OR existing.artifact_count IS DISTINCT FROM (p_manifest ->> 'artifact_count')::bigint
           OR existing.artifact_total_bytes IS DISTINCT FROM (p_manifest ->> 'artifact_total_bytes')::bigint
           OR existing.postgresql_version IS DISTINCT FROM p_manifest ->> 'postgresql_version'
           OR existing.backup_tool_version IS DISTINCT FROM p_manifest ->> 'backup_tool_version'
           OR existing.artifact_verification_tool_version IS DISTINCT FROM p_manifest ->> 'artifact_verification_tool_version'
           OR existing.completeness IS DISTINCT FROM p_completeness
           OR existing.created_at IS DISTINCT FROM (p_manifest ->> 'created_at')::timestamptz THEN
            RAISE EXCEPTION 'recovery set % is already registered with different immutable data', existing.recovery_set_id USING ERRCODE = 'unique_violation';
        END IF;
        RETURN existing.id;
    END IF;
    INSERT INTO dbv2_catalog.backup_sets (
        backup_key, recovery_set_id, alembic_revision,
         quiesce_started_at, quiesce_ended_at, manifest_schema_version,
         database_name, recovery_manifest_artifact_id, recovery_manifest_sha256,
         database_backup_kind, database_backup_artifact_id, database_backup_sha256,
         database_backup_size_bytes, wal_start_lsn, wal_end_lsn,
         artifact_snapshot_manifest_artifact_id, artifact_snapshot_manifest_sha256, artifact_snapshot_manifest_media_type,
         artifact_snapshot_sha256, artifact_count, artifact_total_bytes,
         postgresql_version, backup_tool_version, artifact_verification_tool_version,
         completeness, created_at)
    VALUES (p_manifest ->> 'backup_key', (p_manifest ->> 'recovery_set_id')::uuid,
           p_manifest ->> 'source_alembic_revision', (p_manifest ->> 'quiesce_started_at')::timestamptz,
           (p_manifest ->> 'quiesce_ended_at')::timestamptz, p_manifest ->> 'schema_version',
           p_manifest ->> 'database_name', (p_manifest ->> 'recovery_manifest_artifact_id')::uuid,
           p_manifest ->> 'recovery_manifest_sha256', p_manifest ->> 'database_backup_kind',
           (p_manifest ->> 'database_backup_artifact_id')::uuid, p_manifest ->> 'database_backup_sha256',
           (p_manifest ->> 'database_backup_size_bytes')::bigint, p_manifest ->> 'wal_start_lsn',
           p_manifest ->> 'wal_end_lsn', (p_manifest ->> 'artifact_snapshot_manifest_artifact_id')::uuid,
           p_manifest ->> 'artifact_snapshot_manifest_sha256', (CASE WHEN p_completeness = 'complete' THEN 'application/vnd.minos.artifact-snapshot+json' END),
           p_manifest ->> 'artifact_snapshot_sha256', (p_manifest ->> 'artifact_count')::bigint,
           (p_manifest ->> 'artifact_total_bytes')::bigint, p_manifest ->> 'postgresql_version',
           p_manifest ->> 'backup_tool_version', p_manifest ->> 'artifact_verification_tool_version',
           p_completeness, (p_manifest ->> 'created_at')::timestamptz)
    RETURNING id INTO new_id;
    INSERT INTO dbv2_audit.admin_operations
        (operation_kind, alembic_revision_from, alembic_revision_to, backup_set_id,
         outcome, evidence_hash)
    VALUES ('migration', p_manifest ->> 'source_alembic_revision',
            p_manifest ->> 'source_alembic_revision', new_id, 'succeeded',
            encode(sha256(convert_to(jsonb_build_object(
                'action', 'backup_set.registered',
                'recovery_set_id', p_manifest ->> 'recovery_set_id',
                'backup_key', p_manifest ->> 'backup_key',
                'recovery_manifest_sha256', p_manifest ->> 'recovery_manifest_sha256',
                'completeness', p_completeness)::text, 'UTF8')), 'hex'));
    RETURN new_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_evaluation.enforce_evaluation_run_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state
       AND (coalesce(OLD.state::text, '(null)'), coalesce(NEW.state::text, '(null)')) NOT IN (('pending', 'running'), ('pending', 'failed'), ('running', 'complete'), ('running', 'failed')) THEN
        RAISE EXCEPTION 'forbidden evaluation transition % -> %',
            coalesce(OLD.state::text, '(null)'),
            coalesce(NEW.state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.completed_at IS NOT NULL
       AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
        RAISE EXCEPTION 'completed_at is written exactly once'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_evaluation.record_evaluation_scores(p_run_id uuid, p_scores jsonb)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    run record;
    written bigint := 0;
BEGIN
    SELECT * INTO run FROM dbv2_evaluation.evaluation_runs
        WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND OR run.state <> 'running' THEN
        RAISE EXCEPTION 'evaluation run % is not running', p_run_id
            USING ERRCODE = 'check_violation';
    END IF;
    WITH inserted AS (
        INSERT INTO dbv2_evaluation.evaluation_scores
            (evaluation_run_id, execution_result_id, score, scoring_version)
        SELECT run.id, run.execution_result_id, (e ->> 'score')::double precision,
               e ->> 'scoring_version'
        FROM jsonb_array_elements(p_scores) AS e
        RETURNING 1
    )
    SELECT count(*) INTO written FROM inserted;
    UPDATE dbv2_evaluation.evaluation_runs
        SET state = 'complete', completed_at = now() WHERE id = run.id;
    RETURN written;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.claim_next_job(p_worker_id text, p_lease_seconds integer, p_plan_id uuid)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    claimed uuid;
BEGIN
    IF p_worker_id IS NULL OR length(p_worker_id) = 0 THEN
        RAISE EXCEPTION 'worker id must be non-empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds <= 0 THEN
        RAISE EXCEPTION 'lease_seconds must be positive, got %', p_lease_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT id INTO claimed FROM dbv2_experiments.experiment_jobs
        WHERE status = 'PENDING' AND (p_plan_id IS NULL OR plan_id = p_plan_id)
        ORDER BY created_at, id
        FOR UPDATE SKIP LOCKED LIMIT 1;
    IF claimed IS NULL THEN
        RETURN NULL;
    END IF;
    UPDATE dbv2_experiments.experiment_jobs
        SET status = 'CLAIMED', claimed_by = p_worker_id, claimed_at = now(),
            lease_expires_at = now() + make_interval(secs => p_lease_seconds),
            updated_at = now()
        WHERE id = claimed;
    INSERT INTO dbv2_experiments.job_events
        (job_id, from_status, to_status, actor_role, worker_id)
    VALUES (claimed, 'PENDING', 'CLAIMED', session_user, p_worker_id);
    RETURN claimed;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.enforce_attempt_exclusivity()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    conflicting uuid;
BEGIN
    PERFORM 1 FROM dbv2_experiments.execution_attempts
        WHERE id = NEW.attempt_id FOR UPDATE;
    IF TG_TABLE_NAME = 'execution_results' THEN
        SELECT id INTO conflicting FROM dbv2_experiments.execution_failures
            WHERE attempt_id = NEW.attempt_id LIMIT 1;
    ELSE
        SELECT id INTO conflicting FROM dbv2_experiments.execution_results
            WHERE attempt_id = NEW.attempt_id LIMIT 1;
    END IF;
    IF conflicting IS NOT NULL THEN
        RAISE EXCEPTION 'attempt % already has an outcome row of the other kind',
            NEW.attempt_id USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.enforce_attempt_outcome()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.outcome IS DISTINCT FROM OLD.outcome
       AND (coalesce(OLD.outcome::text, '(null)'), coalesce(NEW.outcome::text, '(null)')) NOT IN (('(null)', 'SUCCEEDED'), ('(null)', 'FAILED'), ('(null)', 'ABANDONED')) THEN
        RAISE EXCEPTION 'forbidden attempt outcome transition % -> %',
            coalesce(OLD.outcome::text, '(null)'),
            coalesce(NEW.outcome::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.outcome IS NOT NULL AND OLD.outcome IS NULL THEN
        IF NEW.finished_at IS NULL OR NEW.runtime_ms IS NULL
           OR NEW.gatk_executable_sha256 IS NULL OR NEW.gatk_version IS NULL THEN
            RAISE EXCEPTION 'the terminal outcome must set every terminal field'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.finished_at < NEW.started_at THEN
            RAISE EXCEPTION 'finished_at may not precede started_at'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.enforce_job_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    attempt_job uuid;
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status
       AND (coalesce(OLD.status::text, '(null)'), coalesce(NEW.status::text, '(null)')) NOT IN (('PENDING', 'CLAIMED'), ('CLAIMED', 'RUNNING'), ('CLAIMED', 'PENDING'), ('RUNNING', 'SUCCEEDED'), ('RUNNING', 'FAILED'), ('RUNNING', 'PENDING')) THEN
        RAISE EXCEPTION 'forbidden job transition % -> %',
            coalesce(OLD.status::text, '(null)'),
            coalesce(NEW.status::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'attempt_count may not decrease (% -> %)',
            OLD.attempt_count, NEW.attempt_count USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'PENDING' AND OLD.status IN ('CLAIMED', 'RUNNING')
       AND (OLD.lease_expires_at IS NULL OR OLD.lease_expires_at >= now()) THEN
        RAISE EXCEPTION 'a held lease may only be released after it expires'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.terminal_attempt_id IS NOT NULL
       AND NEW.terminal_attempt_id IS DISTINCT FROM OLD.terminal_attempt_id THEN
        RAISE EXCEPTION 'terminal_attempt_id is written exactly once'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.terminal_attempt_id IS NOT NULL
       AND NEW.terminal_attempt_id IS DISTINCT FROM OLD.terminal_attempt_id THEN
        SELECT job_id INTO attempt_job FROM dbv2_experiments.execution_attempts
            WHERE id = NEW.terminal_attempt_id;
        IF attempt_job IS DISTINCT FROM NEW.id THEN
            RAISE EXCEPTION 'terminal_attempt_id % belongs to a different job',
                NEW.terminal_attempt_id USING ERRCODE = 'foreign_key_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.enqueue_plan_jobs(p_plan_id uuid, p_max_jobs integer)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    created bigint := 0;
BEGIN
    IF p_max_jobs IS NULL OR p_max_jobs <= 0 THEN
        RAISE EXCEPTION 'p_max_jobs must be positive, got %', p_max_jobs
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM 1 FROM dbv2_experiments.experiment_plans WHERE id = p_plan_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown plan %', p_plan_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    WITH candidate AS (
        SELECT m.id AS member_id, c.id AS config_id,
               encode(sha256(convert_to(p_plan_id::text || ':' || m.id::text || ':'
                                        || c.id::text, 'UTF8')), 'hex') AS job_key
        FROM dbv2_experiments.experiment_plan_members AS m
        JOIN dbv2_experiments.experiment_plan_configs AS c
          ON c.plan_id = m.plan_id
        WHERE m.plan_id = p_plan_id
        ORDER BY m.id, c.id
        LIMIT p_max_jobs
    ),
    inserted AS (
        INSERT INTO dbv2_experiments.experiment_jobs
            (plan_id, plan_member_id, plan_config_id, job_key, status, attempt_count)
        SELECT p_plan_id, member_id, config_id, job_key, 'PENDING', 0 FROM candidate
        ON CONFLICT ON CONSTRAINT uq_jobs_logical_identity DO NOTHING
        RETURNING id
    ),
    logged AS (
        INSERT INTO dbv2_experiments.job_events
            (job_id, from_status, to_status, actor_role)
        SELECT id, NULL, 'PENDING', session_user FROM inserted
        RETURNING 1
    )
    SELECT count(*) INTO created FROM logged;
    RETURN created;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.extend_attempt_lease(p_attempt_id uuid, p_worker_id text, p_lease_seconds integer)
RETURNS timestamptz
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    attempt record;
    job record;
    new_expiry timestamptz;
BEGIN
    SELECT * INTO attempt FROM dbv2_experiments.execution_attempts
        WHERE id = p_attempt_id;
    IF NOT FOUND OR attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'attempt % is not open', p_attempt_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO job FROM dbv2_experiments.experiment_jobs
        WHERE id = attempt.job_id FOR UPDATE;
    IF job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION 'worker % does not hold job %', p_worker_id, job.id
            USING ERRCODE = 'check_violation';
    END IF;
    new_expiry := greatest(job.lease_expires_at,
                           now() + make_interval(secs => p_lease_seconds));
    UPDATE dbv2_experiments.experiment_jobs
        SET lease_expires_at = new_expiry, updated_at = now() WHERE id = job.id;
    RETURN new_expiry;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.persist_experiment_plan(p_plan jsonb)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    new_id uuid;
BEGIN
    SELECT id INTO new_id FROM dbv2_experiments.experiment_plans
        WHERE plan_hash = p_plan ->> 'plan_hash';
    IF FOUND THEN
        RETURN new_id;
    END IF;
    INSERT INTO dbv2_experiments.experiment_plans
        (plan_hash, snapshot_id, candidate_set_id, parameter_space_id, partition,
         member_count, candidate_count, logical_job_count)
    SELECT p_plan ->> 'plan_hash', (p_plan ->> 'snapshot_id')::uuid,
           (p_plan ->> 'candidate_set_id')::uuid,
           (p_plan ->> 'parameter_space_id')::uuid, p_plan ->> 'partition',
           (p_plan ->> 'member_count')::integer,
           (p_plan ->> 'candidate_count')::integer,
           (p_plan ->> 'logical_job_count')::bigint
    RETURNING id INTO new_id;
    INSERT INTO dbv2_experiments.experiment_plan_members
        (plan_id, snapshot_member_id, bam_profile_id, dataset_id, member_index)
    SELECT new_id, (m ->> 'snapshot_member_id')::uuid, (m ->> 'bam_profile_id')::uuid,
           (m ->> 'dataset_id')::uuid, (m ->> 'member_index')::integer
    FROM jsonb_array_elements(p_plan -> 'members') AS m;
    INSERT INTO dbv2_experiments.experiment_plan_configs
        (plan_id, candidate_config_id, config_hash, config_index)
    SELECT new_id, (c ->> 'candidate_config_id')::uuid, c ->> 'config_hash',
           (c ->> 'config_index')::integer
    FROM jsonb_array_elements(p_plan -> 'configs') AS c;
    RETURN new_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.record_attempt_failure(p_attempt_id uuid, p_worker_id text, p_failure jsonb)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    attempt record;
    job record;
    failure_id uuid;
BEGIN
    SELECT * INTO attempt FROM dbv2_experiments.execution_attempts
        WHERE id = p_attempt_id;
    IF NOT FOUND OR attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'attempt % is not open', p_attempt_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO job FROM dbv2_experiments.experiment_jobs
        WHERE id = attempt.job_id FOR UPDATE;
    IF job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION 'worker % does not hold job %', p_worker_id, job.id
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO dbv2_experiments.execution_failures
        (attempt_id, job_id, plan_id, failure_code, exit_code, stderr_sha256)
    SELECT attempt.id, job.id, job.plan_id, p_failure ->> 'failure_code',
           (p_failure ->> 'exit_code')::integer, p_failure ->> 'stderr_sha256'
    RETURNING id INTO failure_id;
    UPDATE dbv2_experiments.execution_attempts
        SET outcome = 'FAILED', finished_at = now(), runtime_ms = 0,
            gatk_executable_sha256 = coalesce(p_failure ->> 'gatk_executable_sha256',
                                              repeat('0', 64)),
            gatk_version = coalesce(p_failure ->> 'gatk_version', 'unknown')
        WHERE id = attempt.id;
    UPDATE dbv2_experiments.experiment_jobs
        SET status = 'FAILED', terminal_attempt_id = attempt.id, updated_at = now()
        WHERE id = job.id;
    INSERT INTO dbv2_experiments.job_events
        (job_id, attempt_id, from_status, to_status, actor_role, worker_id)
    VALUES (job.id, attempt.id, 'RUNNING', 'FAILED', session_user, p_worker_id);
    RETURN failure_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.record_attempt_result(p_attempt_id uuid, p_worker_id text, p_result jsonb)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    attempt record;
    job record;
    result_id uuid;
BEGIN
    SELECT * INTO attempt FROM dbv2_experiments.execution_attempts
        WHERE id = p_attempt_id;
    IF NOT FOUND OR attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'attempt % is not open', p_attempt_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO job FROM dbv2_experiments.experiment_jobs
        WHERE id = attempt.job_id FOR UPDATE;
    IF job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION 'worker % does not hold job %', p_worker_id, job.id
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO dbv2_experiments.execution_results
        (attempt_id, job_id, plan_id, job_key, result_hash, input_identity_hash,
         logical_argv_hash, vcf_artifact_id, manifest_artifact_id)
    SELECT attempt.id, job.id, job.plan_id, job.job_key, p_result ->> 'result_hash',
           p_result ->> 'input_identity_hash', p_result ->> 'logical_argv_hash',
           (p_result ->> 'vcf_artifact_id')::uuid,
           (p_result ->> 'manifest_artifact_id')::uuid
    RETURNING id INTO result_id;
    UPDATE dbv2_experiments.execution_attempts
        SET outcome = 'SUCCEEDED', finished_at = now(),
            runtime_ms = (p_result ->> 'runtime_ms')::bigint,
            gatk_executable_sha256 = p_result ->> 'gatk_executable_sha256',
            gatk_version = p_result ->> 'gatk_version'
        WHERE id = attempt.id;
    UPDATE dbv2_experiments.experiment_jobs
        SET status = 'SUCCEEDED', terminal_attempt_id = attempt.id, updated_at = now()
        WHERE id = job.id;
    INSERT INTO dbv2_experiments.job_events
        (job_id, attempt_id, from_status, to_status, actor_role, worker_id)
    VALUES (job.id, attempt.id, 'RUNNING', 'SUCCEEDED', session_user, p_worker_id);
    RETURN result_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_experiments.start_attempt(p_job_id uuid, p_worker_id text)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    job record;
    attempt_id uuid;
BEGIN
    SELECT * INTO job FROM dbv2_experiments.experiment_jobs
        WHERE id = p_job_id FOR UPDATE;
    IF NOT FOUND OR job.status <> 'CLAIMED' OR job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION 'job % is not CLAIMED by %', p_job_id, p_worker_id
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO dbv2_experiments.execution_attempts
        (job_id, plan_id, attempt_number, worker_id, started_at)
    VALUES (job.id, job.plan_id, job.attempt_count + 1, p_worker_id, now())
    RETURNING id INTO attempt_id;
    UPDATE dbv2_experiments.experiment_jobs
        SET status = 'RUNNING', attempt_count = job.attempt_count + 1, updated_at = now()
        WHERE id = job.id;
    INSERT INTO dbv2_experiments.job_events
        (job_id, attempt_id, from_status, to_status, actor_role, worker_id)
    VALUES (job.id, attempt_id, 'CLAIMED', 'RUNNING', session_user, p_worker_id);
    RETURN attempt_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_models.activate_model_version(p_model_version_id uuid, p_release_id uuid, p_reason text)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    new_id uuid;
BEGIN
    PERFORM 1 FROM dbv2_models.model_versions WHERE id = p_model_version_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown model version %', p_model_version_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    PERFORM 1 FROM dbv2_models.model_activations
        WHERE model_version_id = p_model_version_id AND deactivated_at IS NULL;
    IF FOUND THEN
        SELECT id INTO new_id FROM dbv2_models.model_activations
            WHERE model_version_id = p_model_version_id AND deactivated_at IS NULL;
        RETURN new_id;
    END IF;
    UPDATE dbv2_models.model_activations SET deactivated_at = now()
        WHERE deactivated_at IS NULL;
    INSERT INTO dbv2_models.model_activations
        (model_version_id, release_id, activated_at, activated_by_role, reason)
    VALUES (p_model_version_id, p_release_id, now(), current_user, p_reason)
    RETURNING id INTO new_id;
    RETURN new_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_models.enforce_model_activation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    other_open uuid;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.deactivated_at IS NOT NULL
           AND NEW.deactivated_at IS DISTINCT FROM OLD.deactivated_at THEN
            RAISE EXCEPTION 'deactivated_at is written exactly once and never cleared'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.deactivated_at IS NULL THEN
        SELECT id INTO other_open FROM dbv2_models.model_activations
            WHERE deactivated_at IS NULL AND id <> NEW.id FOR UPDATE;
        IF other_open IS NOT NULL THEN
            RAISE EXCEPTION 'activation % is still open', other_open
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_models.enforce_training_run_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state
       AND (coalesce(OLD.state::text, '(null)'), coalesce(NEW.state::text, '(null)')) NOT IN (('running', 'complete'), ('running', 'failed')) THEN
        RAISE EXCEPTION 'forbidden training transition % -> %',
            coalesce(OLD.state::text, '(null)'),
            coalesce(NEW.state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.completed_at IS NOT NULL
       AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
        RAISE EXCEPTION 'completed_at is written exactly once'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_profiling.enforce_profile_snapshot_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state
       AND (coalesce(OLD.state::text, '(null)'), coalesce(NEW.state::text, '(null)')) NOT IN (('frozen', 'superseded')) THEN
        RAISE EXCEPTION 'forbidden snapshot transition % -> %',
            coalesce(OLD.state::text, '(null)'),
            coalesce(NEW.state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.acquire_lease(p_lease_key text, p_holder text, p_ttl_seconds integer)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    lease record;
    token bigint;
BEGIN
    IF p_ttl_seconds IS NULL OR p_ttl_seconds <= 0 THEN
        RAISE EXCEPTION 'ttl_seconds must be positive, got %', p_ttl_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT * INTO lease FROM dbv2_runtime.leases
        WHERE lease_key = p_lease_key FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO dbv2_runtime.leases
            (lease_key, holder, acquired_at, expires_at, fence_token)
        VALUES (p_lease_key, p_holder, now(),
                now() + make_interval(secs => p_ttl_seconds), 1)
        RETURNING fence_token INTO token;
        RETURN token;
    END IF;
    IF lease.holder <> p_holder AND lease.expires_at > now() THEN
        RAISE EXCEPTION 'lease % is held by % until %',
            p_lease_key, lease.holder, lease.expires_at
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE dbv2_runtime.leases
        SET holder = p_holder, fence_token = lease.fence_token + 1,
            acquired_at = CASE WHEN lease.holder <> p_holder THEN now()
                               ELSE lease.acquired_at END,
            expires_at = CASE WHEN lease.holder <> p_holder
                              THEN now() + make_interval(secs => p_ttl_seconds)
                              ELSE greatest(lease.expires_at,
                                   now() + make_interval(secs => p_ttl_seconds)) END
        WHERE lease_key = p_lease_key
        RETURNING fence_token INTO token;
    RETURN token;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.enforce_active_selection_window()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
DECLARE
    other_open uuid;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.effective_to IS NOT NULL
           AND NEW.effective_to IS DISTINCT FROM OLD.effective_to THEN
            RAISE EXCEPTION 'effective_to is written exactly once and never cleared'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.effective_to IS NOT NULL AND NEW.effective_to < NEW.effective_from THEN
        RAISE EXCEPTION 'effective_to may not precede effective_from'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.effective_to IS NULL THEN
        SELECT id INTO other_open FROM dbv2_runtime.active_selections
            WHERE effective_to IS NULL AND id <> NEW.id FOR UPDATE;
        IF other_open IS NOT NULL THEN
            RAISE EXCEPTION 'selection window % is still open', other_open
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.enforce_lease_transition()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.fence_token <= OLD.fence_token THEN
        RAISE EXCEPTION 'fence_token must strictly increase (% -> %)',
            OLD.fence_token, NEW.fence_token USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.holder IS DISTINCT FROM OLD.holder THEN
        IF OLD.expires_at > now() THEN
            RAISE EXCEPTION 'lease % is held by % until %',
                OLD.lease_key, OLD.holder, OLD.expires_at
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        IF NEW.acquired_at IS DISTINCT FROM OLD.acquired_at THEN
            RAISE EXCEPTION 'acquired_at changes only when the holder changes'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.expires_at < OLD.expires_at THEN
            RAISE EXCEPTION 'expires_at may not move backwards for the same holder'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.enforce_service_instance_state()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $minos$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state
       AND (coalesce(OLD.state::text, '(null)'), coalesce(NEW.state::text, '(null)')) NOT IN (('starting', 'healthy'), ('starting', 'degraded'), ('starting', 'stopped'), ('healthy', 'degraded'), ('healthy', 'stopped'), ('degraded', 'healthy'), ('degraded', 'stopped')) THEN
        RAISE EXCEPTION 'forbidden instance transition % -> %',
            coalesce(OLD.state::text, '(null)'),
            coalesce(NEW.state::text, '(null)')
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.last_heartbeat_at IS NOT NULL AND OLD.last_heartbeat_at IS NOT NULL
       AND NEW.last_heartbeat_at < OLD.last_heartbeat_at THEN
        RAISE EXCEPTION 'last_heartbeat_at may not move backwards'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.release_id IS DISTINCT FROM OLD.release_id AND OLD.state <> 'starting' THEN
        RAISE EXCEPTION 'release_id may only change while the instance is starting'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.register_service_instance(p_instance_key text, p_service_name text, p_release_id uuid)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    existing record;
    new_id uuid;
BEGIN
    SELECT * INTO existing FROM dbv2_runtime.service_instances
        WHERE instance_key = p_instance_key;
    IF FOUND THEN
        IF existing.service_name <> p_service_name THEN
            RAISE EXCEPTION 'instance_key % belongs to service %',
                p_instance_key, existing.service_name
                USING ERRCODE = 'unique_violation';
        END IF;
        RETURN existing.id;
    END IF;
    INSERT INTO dbv2_runtime.service_instances
        (instance_key, service_name, release_id, state, last_heartbeat_at)
    VALUES (p_instance_key, p_service_name, p_release_id, 'starting', now())
    RETURNING id INTO new_id;
    RETURN new_id;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.release_lease(p_lease_key text, p_holder text, p_fence_token bigint)
RETURNS void
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    lease record;
BEGIN
    SELECT * INTO lease FROM dbv2_runtime.leases
        WHERE lease_key = p_lease_key FOR UPDATE;
    IF NOT FOUND OR lease.holder <> p_holder THEN
        RAISE EXCEPTION 'lease % is not held by %', p_lease_key, p_holder
            USING ERRCODE = 'check_violation';
    END IF;
    IF lease.fence_token <> p_fence_token THEN
        RAISE EXCEPTION 'stale fence token % for lease %', p_fence_token, p_lease_key
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE dbv2_runtime.leases
        SET fence_token = lease.fence_token + 1, expires_at = now()
        WHERE lease_key = p_lease_key;
    RETURN;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.renew_lease(p_lease_key text, p_holder text, p_fence_token bigint, p_ttl_seconds integer)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    lease record;
    token bigint;
BEGIN
    SELECT * INTO lease FROM dbv2_runtime.leases
        WHERE lease_key = p_lease_key FOR UPDATE;
    IF NOT FOUND OR lease.holder <> p_holder THEN
        RAISE EXCEPTION 'lease % is not held by %', p_lease_key, p_holder
            USING ERRCODE = 'check_violation';
    END IF;
    IF lease.fence_token <> p_fence_token THEN
        RAISE EXCEPTION 'stale fence token % for lease %', p_fence_token, p_lease_key
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE dbv2_runtime.leases
        SET fence_token = lease.fence_token + 1,
            expires_at = greatest(lease.expires_at,
                                  now() + make_interval(secs => p_ttl_seconds))
        WHERE lease_key = p_lease_key
        RETURNING fence_token INTO token;
    RETURN token;
END
$minos$;""",
    r"""CREATE FUNCTION dbv2_runtime.set_active_selection(p_release_id uuid, p_model_version_id uuid, p_candidate_config_id uuid)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE SECURITY DEFINER
SET search_path = pg_catalog
AS $minos$
DECLARE
    new_id uuid;
BEGIN
    PERFORM 1 FROM dbv2_catalog.releases WHERE id = p_release_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown release %', p_release_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    SELECT id INTO new_id FROM dbv2_runtime.active_selections
        WHERE effective_to IS NULL AND release_id = p_release_id
          AND model_version_id = p_model_version_id
          AND candidate_config_id = p_candidate_config_id;
    IF FOUND THEN
        RETURN new_id;
    END IF;
    UPDATE dbv2_runtime.active_selections SET effective_to = now()
        WHERE effective_to IS NULL;
    INSERT INTO dbv2_runtime.active_selections
        (release_id, model_version_id, candidate_config_id, effective_from,
         selected_by_role)
    VALUES (p_release_id, p_model_version_id, p_candidate_config_id, now(), current_user)
    RETURNING id INTO new_id;
    RETURN new_id;
END
$minos$;""",
    r"""CREATE TRIGGER trg_admin_operations_no_delete
    BEFORE DELETE ON dbv2_audit.admin_operations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_admin_operations_no_update
    BEFORE UPDATE ON dbv2_audit.admin_operations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_events_no_delete
    BEFORE DELETE ON dbv2_audit.events
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_events_no_update
    BEFORE UPDATE ON dbv2_audit.events
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_artifact_locations_immutable_columns
    BEFORE UPDATE ON dbv2_catalog.artifact_locations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'artifact_id', 'backend_id', 'object_key', 'created_at');""",
    r"""CREATE TRIGGER trg_artifact_locations_no_delete
    BEFORE DELETE ON dbv2_catalog.artifact_locations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_artifact_locations_state
    BEFORE INSERT OR UPDATE ON dbv2_catalog.artifact_locations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_catalog.enforce_artifact_location_state();""",
    r"""CREATE TRIGGER trg_artifacts_immutable_columns
    BEFORE UPDATE ON dbv2_catalog.artifacts
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'artifact_kind', 'content_sha256', 'size_bytes', 'media_type', 'schema_version', 'storage_mode', 'inline_payload', 'backup_scope', 'provenance', 'created_at');""",
    r"""CREATE TRIGGER trg_artifacts_lifecycle
    BEFORE UPDATE ON dbv2_catalog.artifacts
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_catalog.enforce_artifact_lifecycle();""",
    r"""CREATE TRIGGER trg_artifacts_no_delete
    BEFORE DELETE ON dbv2_catalog.artifacts
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_backup_sets_immutable_columns
    BEFORE UPDATE ON dbv2_catalog.backup_sets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_catalog.enforce_backup_set_immutability();""",
    r"""CREATE TRIGGER trg_backup_sets_no_delete
    BEFORE DELETE ON dbv2_catalog.backup_sets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE CONSTRAINT TRIGGER trg_backup_sets_shape
    AFTER INSERT OR UPDATE ON dbv2_catalog.backup_sets DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_catalog.enforce_backup_set_shape();""",
    r"""CREATE TRIGGER trg_datasets_no_delete
    BEFORE DELETE ON dbv2_catalog.datasets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_datasets_no_update
    BEFORE UPDATE ON dbv2_catalog.datasets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_releases_immutable_columns
    BEFORE UPDATE ON dbv2_catalog.releases
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'release_key', 'release_hash', 'component_manifest', 'created_by_role', 'created_at');""",
    r"""CREATE TRIGGER trg_releases_no_delete
    BEFORE DELETE ON dbv2_catalog.releases
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_releases_state
    BEFORE UPDATE ON dbv2_catalog.releases
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_catalog.enforce_release_state();""",
    r"""CREATE TRIGGER trg_storage_backends_immutable_columns
    BEFORE UPDATE ON dbv2_catalog.storage_backends
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'backend_key', 'backend_type', 'logical_root', 'created_at');""",
    r"""CREATE TRIGGER trg_storage_backends_no_delete
    BEFORE DELETE ON dbv2_catalog.storage_backends
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_evaluation_metrics_no_delete
    BEFORE DELETE ON dbv2_evaluation.evaluation_metrics
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_evaluation_metrics_no_update
    BEFORE UPDATE ON dbv2_evaluation.evaluation_metrics
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_evaluation_runs_immutable_columns
    BEFORE UPDATE ON dbv2_evaluation.evaluation_runs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'execution_result_id', 'truth_binding_id', 'evaluator_version', 'evaluation_hash', 'created_at');""",
    r"""CREATE TRIGGER trg_evaluation_runs_no_delete
    BEFORE DELETE ON dbv2_evaluation.evaluation_runs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_evaluation_runs_state
    BEFORE UPDATE ON dbv2_evaluation.evaluation_runs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_evaluation.enforce_evaluation_run_state();""",
    r"""CREATE TRIGGER trg_evaluation_scores_no_delete
    BEFORE DELETE ON dbv2_evaluation.evaluation_scores
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_evaluation_scores_no_update
    BEFORE UPDATE ON dbv2_evaluation.evaluation_scores
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_truth_bindings_no_delete
    BEFORE DELETE ON dbv2_evaluation.truth_bindings
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_truth_bindings_no_update
    BEFORE UPDATE ON dbv2_evaluation.truth_bindings
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_candidate_configs_no_delete
    BEFORE DELETE ON dbv2_experiments.candidate_configs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_candidate_configs_no_update
    BEFORE UPDATE ON dbv2_experiments.candidate_configs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_candidate_set_configs_no_delete
    BEFORE DELETE ON dbv2_experiments.candidate_set_configs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_candidate_set_configs_no_update
    BEFORE UPDATE ON dbv2_experiments.candidate_set_configs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_candidate_sets_no_delete
    BEFORE DELETE ON dbv2_experiments.candidate_sets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_candidate_sets_no_update
    BEFORE UPDATE ON dbv2_experiments.candidate_sets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_execution_attempts_immutable_columns
    BEFORE UPDATE ON dbv2_experiments.execution_attempts
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'job_id', 'plan_id', 'attempt_number', 'worker_id', 'started_at', 'created_at');""",
    r"""CREATE TRIGGER trg_execution_attempts_no_delete
    BEFORE DELETE ON dbv2_experiments.execution_attempts
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_execution_attempts_outcome
    BEFORE UPDATE ON dbv2_experiments.execution_attempts
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_experiments.enforce_attempt_outcome();""",
    r"""CREATE TRIGGER trg_execution_failures_exclusivity
    BEFORE INSERT ON dbv2_experiments.execution_failures
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_experiments.enforce_attempt_exclusivity();""",
    r"""CREATE TRIGGER trg_execution_failures_no_delete
    BEFORE DELETE ON dbv2_experiments.execution_failures
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_execution_failures_no_update
    BEFORE UPDATE ON dbv2_experiments.execution_failures
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_execution_results_exclusivity
    BEFORE INSERT ON dbv2_experiments.execution_results
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_experiments.enforce_attempt_exclusivity();""",
    r"""CREATE TRIGGER trg_execution_results_no_delete
    BEFORE DELETE ON dbv2_experiments.execution_results
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_execution_results_no_update
    BEFORE UPDATE ON dbv2_experiments.execution_results
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_experiment_jobs_immutable_columns
    BEFORE UPDATE ON dbv2_experiments.experiment_jobs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'plan_id', 'plan_member_id', 'plan_config_id', 'job_key', 'created_at');""",
    r"""CREATE TRIGGER trg_experiment_jobs_no_delete
    BEFORE DELETE ON dbv2_experiments.experiment_jobs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_experiment_jobs_state
    BEFORE UPDATE ON dbv2_experiments.experiment_jobs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_experiments.enforce_job_state();""",
    r"""CREATE TRIGGER trg_experiment_plan_configs_no_delete
    BEFORE DELETE ON dbv2_experiments.experiment_plan_configs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_experiment_plan_configs_no_update
    BEFORE UPDATE ON dbv2_experiments.experiment_plan_configs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_experiment_plan_members_no_delete
    BEFORE DELETE ON dbv2_experiments.experiment_plan_members
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_experiment_plan_members_no_update
    BEFORE UPDATE ON dbv2_experiments.experiment_plan_members
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_experiment_plans_no_delete
    BEFORE DELETE ON dbv2_experiments.experiment_plans
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_experiment_plans_no_update
    BEFORE UPDATE ON dbv2_experiments.experiment_plans
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_job_events_no_delete
    BEFORE DELETE ON dbv2_experiments.job_events
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_job_events_no_update
    BEFORE UPDATE ON dbv2_experiments.job_events
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_parameter_spaces_no_delete
    BEFORE DELETE ON dbv2_experiments.parameter_spaces
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_parameter_spaces_no_update
    BEFORE UPDATE ON dbv2_experiments.parameter_spaces
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_model_activations
    BEFORE INSERT OR UPDATE ON dbv2_models.model_activations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_models.enforce_model_activation();""",
    r"""CREATE TRIGGER trg_model_activations_immutable_columns
    BEFORE UPDATE ON dbv2_models.model_activations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'model_version_id', 'release_id', 'activated_at', 'activated_by_role', 'reason');""",
    r"""CREATE TRIGGER trg_model_activations_no_delete
    BEFORE DELETE ON dbv2_models.model_activations
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_model_definitions_no_delete
    BEFORE DELETE ON dbv2_models.model_definitions
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_model_definitions_no_update
    BEFORE UPDATE ON dbv2_models.model_definitions
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_model_versions_no_delete
    BEFORE DELETE ON dbv2_models.model_versions
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_model_versions_no_update
    BEFORE UPDATE ON dbv2_models.model_versions
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_training_runs_immutable_columns
    BEFORE UPDATE ON dbv2_models.training_runs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'model_definition_id', 'train_matrix_id', 'validation_matrix_id', 'trainer_version', 'training_identity_hash', 'created_at');""",
    r"""CREATE TRIGGER trg_training_runs_no_delete
    BEFORE DELETE ON dbv2_models.training_runs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_training_runs_state
    BEFORE UPDATE ON dbv2_models.training_runs
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_models.enforce_training_run_state();""",
    r"""CREATE TRIGGER trg_bam_profiles_no_delete
    BEFORE DELETE ON dbv2_profiling.bam_profiles
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_bam_profiles_no_update
    BEFORE UPDATE ON dbv2_profiling.bam_profiles
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_feature_matrices_no_delete
    BEFORE DELETE ON dbv2_profiling.feature_matrices
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_feature_matrices_no_update
    BEFORE UPDATE ON dbv2_profiling.feature_matrices
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_feature_matrix_members_no_delete
    BEFORE DELETE ON dbv2_profiling.feature_matrix_members
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_feature_matrix_members_no_update
    BEFORE UPDATE ON dbv2_profiling.feature_matrix_members
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_feature_sets_no_delete
    BEFORE DELETE ON dbv2_profiling.feature_sets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_feature_sets_no_update
    BEFORE UPDATE ON dbv2_profiling.feature_sets
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_profile_snapshot_members_no_delete
    BEFORE DELETE ON dbv2_profiling.profile_snapshot_members
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_profile_snapshot_members_no_update
    BEFORE UPDATE ON dbv2_profiling.profile_snapshot_members
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_update();""",
    r"""CREATE TRIGGER trg_profile_snapshots_immutable_columns
    BEFORE UPDATE ON dbv2_profiling.profile_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'snapshot_key', 'epoch', 'parent_snapshot_id', 'split_algorithm_version', 'member_count', 'snapshot_hash', 'created_at');""",
    r"""CREATE TRIGGER trg_profile_snapshots_no_delete
    BEFORE DELETE ON dbv2_profiling.profile_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_profile_snapshots_state
    BEFORE UPDATE ON dbv2_profiling.profile_snapshots
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_profiling.enforce_profile_snapshot_state();""",
    r"""CREATE TRIGGER trg_active_selections_immutable_columns
    BEFORE UPDATE ON dbv2_runtime.active_selections
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'release_id', 'model_version_id', 'candidate_config_id', 'effective_from', 'selected_by_role');""",
    r"""CREATE TRIGGER trg_active_selections_no_delete
    BEFORE DELETE ON dbv2_runtime.active_selections
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_active_selections_window
    BEFORE INSERT OR UPDATE ON dbv2_runtime.active_selections
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_runtime.enforce_active_selection_window();""",
    r"""CREATE TRIGGER trg_leases_immutable_columns
    BEFORE UPDATE ON dbv2_runtime.leases
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'lease_key');""",
    r"""CREATE TRIGGER trg_leases_no_delete
    BEFORE DELETE ON dbv2_runtime.leases
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_leases_transition
    BEFORE UPDATE ON dbv2_runtime.leases
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_runtime.enforce_lease_transition();""",
    r"""CREATE TRIGGER trg_service_instances_immutable_columns
    BEFORE UPDATE ON dbv2_runtime.service_instances
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_immutable_column_update('id', 'instance_key', 'service_name', 'created_at');""",
    r"""CREATE TRIGGER trg_service_instances_no_delete
    BEFORE DELETE ON dbv2_runtime.service_instances
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_audit.reject_delete();""",
    r"""CREATE TRIGGER trg_service_instances_state
    BEFORE UPDATE ON dbv2_runtime.service_instances
    FOR EACH ROW
    EXECUTE FUNCTION dbv2_runtime.enforce_service_instance_state();""",
    r"""REVOKE ALL ON SCHEMA dbv2_audit FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_audit REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_audit REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_audit REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON SCHEMA dbv2_catalog FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_catalog REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_catalog REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_catalog REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON SCHEMA dbv2_evaluation FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_evaluation REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_evaluation REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_evaluation REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON SCHEMA dbv2_experiments FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_experiments REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_experiments REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_experiments REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON SCHEMA dbv2_models FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_models REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_models REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_models REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON SCHEMA dbv2_profiling FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_profiling REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_profiling REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_profiling REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON SCHEMA dbv2_runtime FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_runtime REVOKE ALL ON TABLES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_runtime REVOKE ALL ON SEQUENCES FROM PUBLIC;""",
    r"""ALTER DEFAULT PRIVILEGES FOR ROLE minos_owner IN SCHEMA dbv2_runtime REVOKE ALL ON FUNCTIONS FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_audit.reject_delete() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_audit.reject_immutable_column_update() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_audit.reject_update() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.enforce_artifact_lifecycle() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.enforce_artifact_location_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.enforce_backup_set_immutability() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.enforce_backup_set_shape() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.enforce_release_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_catalog.register_backup_set(p_manifest jsonb, p_completeness text) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_evaluation.enforce_evaluation_run_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_evaluation.record_evaluation_scores(p_run_id uuid, p_scores jsonb) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.claim_next_job(p_worker_id text, p_lease_seconds integer, p_plan_id uuid) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.enforce_attempt_exclusivity() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.enforce_attempt_outcome() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.enforce_job_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.enqueue_plan_jobs(p_plan_id uuid, p_max_jobs integer) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.extend_attempt_lease(p_attempt_id uuid, p_worker_id text, p_lease_seconds integer) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.persist_experiment_plan(p_plan jsonb) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.record_attempt_failure(p_attempt_id uuid, p_worker_id text, p_failure jsonb) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.record_attempt_result(p_attempt_id uuid, p_worker_id text, p_result jsonb) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_experiments.start_attempt(p_job_id uuid, p_worker_id text) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_models.activate_model_version(p_model_version_id uuid, p_release_id uuid, p_reason text) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_models.enforce_model_activation() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_models.enforce_training_run_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_profiling.enforce_profile_snapshot_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.acquire_lease(p_lease_key text, p_holder text, p_ttl_seconds integer) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.enforce_active_selection_window() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.enforce_lease_transition() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.enforce_service_instance_state() FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.register_service_instance(p_instance_key text, p_service_name text, p_release_id uuid) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.release_lease(p_lease_key text, p_holder text, p_fence_token bigint) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.renew_lease(p_lease_key text, p_holder text, p_fence_token bigint, p_ttl_seconds integer) FROM PUBLIC;""",
    r"""REVOKE ALL ON FUNCTION dbv2_runtime.set_active_selection(p_release_id uuid, p_model_version_id uuid, p_candidate_config_id uuid) FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_audit.admin_operations FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_audit.events FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_catalog.artifact_locations FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_catalog.artifacts FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_catalog.backup_sets FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_catalog.datasets FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_catalog.releases FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_catalog.storage_backends FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_evaluation.evaluation_metrics FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_evaluation.evaluation_runs FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_evaluation.evaluation_scores FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_evaluation.truth_bindings FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.candidate_configs FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.candidate_set_configs FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.candidate_sets FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.execution_attempts FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.execution_failures FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.execution_results FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.experiment_jobs FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.experiment_plan_configs FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.experiment_plan_members FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.experiment_plans FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.job_events FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_experiments.parameter_spaces FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_models.model_activations FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_models.model_definitions FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_models.model_versions FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_models.training_runs FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_profiling.bam_profiles FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_profiling.feature_matrices FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_profiling.feature_matrix_members FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_profiling.feature_sets FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_profiling.profile_snapshot_members FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_profiling.profile_snapshots FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_runtime.active_selections FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_runtime.leases FROM PUBLIC;""",
    r"""REVOKE ALL ON TABLE dbv2_runtime.service_instances FROM PUBLIC;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_audit.reject_delete() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_audit.reject_immutable_column_update() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_audit.reject_update() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.enforce_artifact_lifecycle() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.enforce_artifact_location_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.enforce_backup_set_immutability() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.enforce_backup_set_shape() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.enforce_release_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean) TO minos_evaluator;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean) TO minos_planner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_artifact_location(p_artifact_id uuid, p_backend_key text, p_object_key text, p_is_primary boolean) TO minos_trainer;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_evaluator;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_planner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_external_artifact(p_content_sha256 char(64), p_size_bytes bigint, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_trainer;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_evaluator;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_planner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.get_or_verify_inline_artifact(p_payload bytea, p_media_type text, p_artifact_kind text, p_backup_scope text, p_retention_class text, p_schema_version text, p_provenance jsonb) TO minos_trainer;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) TO minos_evaluator;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) TO minos_planner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) TO minos_trainer;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.record_artifact_verification(p_artifact_id uuid, p_observed_sha256 char(64), p_observed_size_bytes bigint, p_location_id uuid) TO minos_verifier;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_catalog.register_backup_set(p_manifest jsonb, p_completeness text) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_evaluation.enforce_evaluation_run_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_evaluation.record_evaluation_scores(p_run_id uuid, p_scores jsonb) TO minos_evaluator;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_evaluation.record_evaluation_scores(p_run_id uuid, p_scores jsonb) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.claim_next_job(p_worker_id text, p_lease_seconds integer, p_plan_id uuid) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.claim_next_job(p_worker_id text, p_lease_seconds integer, p_plan_id uuid) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.enforce_attempt_exclusivity() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.enforce_attempt_outcome() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.enforce_job_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.enqueue_plan_jobs(p_plan_id uuid, p_max_jobs integer) TO minos_enqueue;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.enqueue_plan_jobs(p_plan_id uuid, p_max_jobs integer) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.extend_attempt_lease(p_attempt_id uuid, p_worker_id text, p_lease_seconds integer) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.extend_attempt_lease(p_attempt_id uuid, p_worker_id text, p_lease_seconds integer) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.persist_experiment_plan(p_plan jsonb) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.persist_experiment_plan(p_plan jsonb) TO minos_planner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.record_attempt_failure(p_attempt_id uuid, p_worker_id text, p_failure jsonb) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.record_attempt_failure(p_attempt_id uuid, p_worker_id text, p_failure jsonb) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.record_attempt_result(p_attempt_id uuid, p_worker_id text, p_result jsonb) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.record_attempt_result(p_attempt_id uuid, p_worker_id text, p_result jsonb) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.start_attempt(p_job_id uuid, p_worker_id text) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_experiments.start_attempt(p_job_id uuid, p_worker_id text) TO minos_runner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_models.activate_model_version(p_model_version_id uuid, p_release_id uuid, p_reason text) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_models.enforce_model_activation() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_models.enforce_training_run_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_profiling.enforce_profile_snapshot_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.acquire_lease(p_lease_key text, p_holder text, p_ttl_seconds integer) TO minos_live;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.acquire_lease(p_lease_key text, p_holder text, p_ttl_seconds integer) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.enforce_active_selection_window() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.enforce_lease_transition() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.enforce_service_instance_state() TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.register_service_instance(p_instance_key text, p_service_name text, p_release_id uuid) TO minos_live;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.register_service_instance(p_instance_key text, p_service_name text, p_release_id uuid) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.release_lease(p_lease_key text, p_holder text, p_fence_token bigint) TO minos_live;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.release_lease(p_lease_key text, p_holder text, p_fence_token bigint) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.renew_lease(p_lease_key text, p_holder text, p_fence_token bigint, p_ttl_seconds integer) TO minos_live;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.renew_lease(p_lease_key text, p_holder text, p_fence_token bigint, p_ttl_seconds integer) TO minos_owner;""",
    r"""GRANT EXECUTE ON FUNCTION dbv2_runtime.set_active_selection(p_release_id uuid, p_model_version_id uuid, p_candidate_config_id uuid) TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_audit TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_audit TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_audit TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_audit TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_audit TO minos_verifier;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_evaluator;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_live;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_catalog TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_catalog TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_planner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_runner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_trainer;""",
    r"""GRANT USAGE ON SCHEMA dbv2_catalog TO minos_verifier;""",
    r"""GRANT USAGE ON SCHEMA dbv2_evaluation TO minos_evaluator;""",
    r"""GRANT USAGE ON SCHEMA dbv2_evaluation TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_evaluation TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_evaluation TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_evaluation TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_evaluation TO minos_trainer;""",
    r"""GRANT USAGE ON SCHEMA dbv2_evaluation TO minos_verifier;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_enqueue;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_evaluator;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_experiments TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_experiments TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_planner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_runner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_experiments TO minos_verifier;""",
    r"""GRANT USAGE ON SCHEMA dbv2_models TO minos_live;""",
    r"""GRANT USAGE ON SCHEMA dbv2_models TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_models TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_models TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_models TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_models TO minos_trainer;""",
    r"""GRANT USAGE ON SCHEMA dbv2_models TO minos_verifier;""",
    r"""GRANT USAGE ON SCHEMA dbv2_profiling TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_profiling TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_profiling TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_profiling TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_profiling TO minos_planner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_profiling TO minos_runner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_profiling TO minos_trainer;""",
    r"""GRANT USAGE ON SCHEMA dbv2_profiling TO minos_verifier;""",
    r"""GRANT USAGE ON SCHEMA dbv2_runtime TO minos_live;""",
    r"""GRANT USAGE ON SCHEMA dbv2_runtime TO minos_migrate;""",
    r"""GRANT CREATE ON SCHEMA dbv2_runtime TO minos_migrate;""",
    r"""GRANT USAGE ON SCHEMA dbv2_runtime TO minos_owner;""",
    r"""GRANT CREATE ON SCHEMA dbv2_runtime TO minos_owner;""",
    r"""GRANT USAGE ON SCHEMA dbv2_runtime TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_audit.admin_operations TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_audit.admin_operations TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_audit.events TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_audit.events TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifact_locations TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_catalog.artifact_locations TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifact_locations TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifact_locations TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifact_locations TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifact_locations TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifacts TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_catalog.artifacts TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifacts TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifacts TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifacts TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.artifacts TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_catalog.backup_sets TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.backup_sets TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.backup_sets TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_catalog.datasets TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.datasets TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.datasets TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.datasets TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.datasets TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.releases TO minos_live;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_catalog.releases TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.releases TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.releases TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_catalog.storage_backends TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.storage_backends TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.storage_backends TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_catalog.storage_backends TO minos_verifier;""",
    r"""GRANT SELECT, INSERT ON TABLE dbv2_evaluation.evaluation_metrics TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_evaluation.evaluation_metrics TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.evaluation_metrics TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.evaluation_metrics TO minos_verifier;""",
    r"""GRANT SELECT, INSERT ON TABLE dbv2_evaluation.evaluation_runs TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_evaluation.evaluation_runs TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.evaluation_runs TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.evaluation_runs TO minos_verifier;""",
    r"""GRANT SELECT, INSERT ON TABLE dbv2_evaluation.evaluation_scores TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_evaluation.evaluation_scores TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.evaluation_scores TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.evaluation_scores TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_evaluation.truth_bindings TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_evaluation.truth_bindings TO minos_owner;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.candidate_configs TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_configs TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_configs TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_configs TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.candidate_set_configs TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_set_configs TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_set_configs TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_set_configs TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.candidate_sets TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_sets TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_sets TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.candidate_sets TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_attempts TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.execution_attempts TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_attempts TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_attempts TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_attempts TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.execution_failures TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_failures TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_failures TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_results TO minos_evaluator;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.execution_results TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_results TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.execution_results TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.experiment_jobs TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_jobs TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_jobs TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_jobs TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.experiment_plan_configs TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plan_configs TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plan_configs TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plan_configs TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.experiment_plan_members TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plan_members TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plan_members TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plan_members TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.experiment_plans TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plans TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plans TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.experiment_plans TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.job_events TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.job_events TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.job_events TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_experiments.parameter_spaces TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.parameter_spaces TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.parameter_spaces TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_experiments.parameter_spaces TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_models.model_activations TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_models.model_activations TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_models.model_activations TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_models.model_definitions TO minos_owner;""",
    r"""GRANT SELECT, INSERT ON TABLE dbv2_models.model_definitions TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_models.model_definitions TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_models.model_versions TO minos_live;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_models.model_versions TO minos_owner;""",
    r"""GRANT SELECT, INSERT ON TABLE dbv2_models.model_versions TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_models.model_versions TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_models.training_runs TO minos_owner;""",
    r"""GRANT SELECT, INSERT ON TABLE dbv2_models.training_runs TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_models.training_runs TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_profiling.bam_profiles TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.bam_profiles TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.bam_profiles TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.bam_profiles TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.bam_profiles TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_profiling.feature_matrices TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrices TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrices TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrices TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrices TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_profiling.feature_matrix_members TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrix_members TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrix_members TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrix_members TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_matrix_members TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_profiling.feature_sets TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_sets TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_sets TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_sets TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.feature_sets TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_profiling.profile_snapshot_members TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshot_members TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshot_members TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshot_members TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshot_members TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_profiling.profile_snapshots TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshots TO minos_planner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshots TO minos_runner;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshots TO minos_trainer;""",
    r"""GRANT SELECT ON TABLE dbv2_profiling.profile_snapshots TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_runtime.active_selections TO minos_live;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_runtime.active_selections TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_runtime.active_selections TO minos_verifier;""",
    r"""GRANT SELECT ON TABLE dbv2_runtime.leases TO minos_live;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_runtime.leases TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_runtime.leases TO minos_verifier;""",
    r"""GRANT SELECT, INSERT, UPDATE ON TABLE dbv2_runtime.service_instances TO minos_live;""",
    r"""GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE dbv2_runtime.service_instances TO minos_owner;""",
    r"""GRANT SELECT ON TABLE dbv2_runtime.service_instances TO minos_verifier;""",
    r"""SET LOCAL ROLE NONE;""",
)

#: every statement of the reverse migration, in execution order.
DOWNGRADE = (
    r"""DO $preflight$
DECLARE
    acting_session text := session_user;
    acting_current text := current_user;
    missing text;
    offender text;
BEGIN
    -- 1. the ORIGINAL migration identity, recorded before any elevation
    RAISE NOTICE 'dbv2 preflight: session_user=% current_user=%',
        acting_session, acting_current;
    -- 2. every required role exists
    SELECT r INTO missing FROM unnest(ARRAY[
        'minos_enqueue',
        'minos_evaluator',
        'minos_live',
        'minos_migrate',
        'minos_owner',
        'minos_planner',
        'minos_runner',
        'minos_trainer',
        'minos_verifier'
    ]) AS r
        WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) LIMIT 1;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'required role % does not exist; 0009 creates no cluster role',
            missing USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 3. the declared LOGIN/NOLOGIN configuration
    SELECT rolname INTO offender FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'minos_enqueue',
            'minos_evaluator',
            'minos_live',
            'minos_migrate',
            'minos_planner',
            'minos_runner',
            'minos_trainer',
            'minos_verifier'
        ]) AND NOT rolcanlogin LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'role % must be LOGIN', offender
            USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    SELECT rolname INTO offender FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'minos_owner'
        ]) AND rolcanlogin LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'role % must be NOLOGIN', offender
            USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 4. the migration identity is a member of the NOLOGIN definer principal
    IF NOT pg_has_role(acting_session, 'minos_owner', 'MEMBER') THEN
        RAISE EXCEPTION 'migration identity % is not a member of minos_owner',
            acting_session USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 5. no required role carries a cluster-wide privilege
    SELECT rolname INTO offender FROM pg_roles
        WHERE rolname = ANY(ARRAY[
            'minos_enqueue',
            'minos_evaluator',
            'minos_live',
            'minos_migrate',
            'minos_owner',
            'minos_planner',
            'minos_runner',
            'minos_trainer',
            'minos_verifier'
        ]) AND (rolsuper OR rolcreaterole OR rolcreatedb) LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'role % must not hold SUPERUSER, CREATEROLE or CREATEDB',
            offender USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    -- 6. the definer principal may create schemas in THIS database
    IF NOT has_database_privilege('minos_owner', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'minos_owner may not create schemas in %; provision the database grant before migrating', current_database()
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- the shared Alembic table is verified, never altered
    PERFORM 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'alembic_version'
          AND c.relkind = 'r';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'public.alembic_version is missing'
            USING ERRCODE = 'undefined_table';
    END IF;
END
$preflight$;""",
    r"""SET LOCAL ROLE minos_owner;""",
    r"""DO $elevated$
BEGIN
    IF current_user <> 'minos_owner' THEN
        RAISE EXCEPTION 'elevation failed: current_user is %, expected minos_owner',
            current_user USING ERRCODE = 'invalid_authorization_specification';
    END IF;
END
$elevated$;""",
    r"""DROP SCHEMA IF EXISTS dbv2_runtime CASCADE;""",
    r"""DROP SCHEMA IF EXISTS dbv2_profiling CASCADE;""",
    r"""DROP SCHEMA IF EXISTS dbv2_models CASCADE;""",
    r"""DROP SCHEMA IF EXISTS dbv2_experiments CASCADE;""",
    r"""DROP SCHEMA IF EXISTS dbv2_evaluation CASCADE;""",
    r"""DROP SCHEMA IF EXISTS dbv2_catalog CASCADE;""",
    r"""DROP SCHEMA IF EXISTS dbv2_audit CASCADE;""",
    r"""SET LOCAL ROLE NONE;""",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
