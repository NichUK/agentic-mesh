CREATE TABLE agentic_mesh_v5.project_runtime_releases (
    project_id text NOT NULL,
    release_id text NOT NULL CHECK (release_id ~ '^[0-9a-f]{64}$'),
    manifest_digest text NOT NULL,
    source_revision text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    image_ref text NOT NULL CHECK (
        image_ref ~ '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'
    ),
    minimum_database_version integer NOT NULL CHECK (minimum_database_version > 0),
    maximum_database_version integer NOT NULL CHECK (
        maximum_database_version >= minimum_database_version
    ),
    build_evidence_ref text NOT NULL CHECK (btrim(build_evidence_ref) <> ''),
    test_evidence_ref text NOT NULL CHECK (btrim(test_evidence_ref) <> ''),
    status text NOT NULL CHECK (
        status IN ('verified', 'deployed', 'retired', 'rejected')
    ),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deployed_at timestamptz,
    PRIMARY KEY (project_id, release_id),
    UNIQUE (project_id, image_ref),
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(
            project_id, manifest_digest
        ) ON DELETE RESTRICT
);

CREATE TABLE agentic_mesh_v5.project_runtime_deployment_attempts (
    project_id text NOT NULL,
    operation_id text NOT NULL CHECK (btrim(operation_id) <> ''),
    source_revision text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    candidate_release_id text,
    previous_image_ref text,
    previous_minimum_database_version integer,
    previous_maximum_database_version integer,
    status text NOT NULL CHECK (
        status IN (
            'building', 'deploying', 'verifying', 'rolling_back',
            'deployed', 'rejected', 'rolled_back', 'failed'
        )
    ),
    error text,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(evidence) = 'object'),
    started_by text NOT NULL CHECK (btrim(started_by) <> ''),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (project_id, operation_id),
    FOREIGN KEY (project_id, candidate_release_id)
        REFERENCES agentic_mesh_v5.project_runtime_releases(
            project_id, release_id
        ) ON DELETE RESTRICT,
    CHECK (
        (candidate_release_id IS NULL AND previous_image_ref IS NULL)
        OR (
            candidate_release_id IS NOT NULL
            AND previous_image_ref ~ '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'
            AND previous_minimum_database_version > 0
            AND previous_maximum_database_version
                >= previous_minimum_database_version
        )
    ),
    CHECK (
        status IN ('building', 'rejected')
        OR candidate_release_id IS NOT NULL
    ),
    CHECK (
        (status IN ('building', 'deploying', 'verifying', 'rolling_back')
            AND completed_at IS NULL)
        OR (status IN ('deployed', 'rejected', 'rolled_back', 'failed')
            AND completed_at IS NOT NULL)
    )
);

CREATE INDEX project_runtime_deployment_status_idx
    ON agentic_mesh_v5.project_runtime_deployment_attempts(project_id, status);
