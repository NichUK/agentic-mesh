ALTER TABLE agentic_mesh_v5.projects
    ADD COLUMN organization_id text NOT NULL DEFAULT 'default'
        CHECK (btrim(organization_id) <> '');

CREATE TABLE agentic_mesh_v5.shared_memory_entries (
    memory_id text PRIMARY KEY CHECK (memory_id ~ '^mem-[0-9a-f]{32}$'),
    scope text NOT NULL CHECK (scope IN ('project_role', 'project', 'organization_role')),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    project_id text,
    role_id text,
    subject text NOT NULL CHECK (btrim(subject) <> ''),
    summary text NOT NULL CHECK (btrim(summary) <> ''),
    tags jsonb NOT NULL CHECK (jsonb_typeof(tags) = 'array'),
    source_kind text NOT NULL CHECK (
        source_kind IN ('document', 'work_item', 'event', 'decision', 'policy', 'release')
    ),
    source_ref text NOT NULL CHECK (
        (source_kind = 'document' AND source_ref LIKE 'document://_%')
        OR (source_kind = 'work_item' AND source_ref LIKE 'work-item://_%')
        OR (source_kind = 'event' AND source_ref LIKE 'event://_%')
        OR (source_kind = 'decision' AND source_ref LIKE 'decision://_%')
        OR (source_kind = 'policy' AND source_ref LIKE 'policy://_%')
        OR (source_kind = 'release' AND source_ref LIKE 'release://_%')
    ),
    observed_source_version text NOT NULL CHECK (btrim(observed_source_version) <> ''),
    source_state text NOT NULL CHECK (source_state IN ('current', 'stale', 'removed')),
    current_source_version text,
    status text NOT NULL CHECK (status IN ('active', 'retired')),
    version integer NOT NULL CHECK (version > 0),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    updated_by text NOT NULL CHECK (btrim(updated_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE RESTRICT,
    CHECK (
        (scope = 'project_role' AND project_id IS NOT NULL AND role_id IS NOT NULL)
        OR (scope = 'project' AND project_id IS NOT NULL AND role_id IS NULL)
        OR (scope = 'organization_role' AND project_id IS NULL AND role_id IS NOT NULL)
    ),
    CHECK (
        (source_state = 'removed' AND current_source_version IS NULL)
        OR (source_state IN ('current', 'stale') AND current_source_version IS NOT NULL)
    ),
    UNIQUE NULLS NOT DISTINCT
        (scope, organization_id, project_id, role_id, subject),
    UNIQUE (memory_id, version)
);

CREATE INDEX shared_memory_visibility_idx
    ON agentic_mesh_v5.shared_memory_entries(
        organization_id, project_id, role_id, scope, status, source_state
    );

CREATE TABLE agentic_mesh_v5.shared_memory_revisions (
    memory_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    operation_id text NOT NULL CHECK (btrim(operation_id) <> ''),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    action text NOT NULL CHECK (action IN ('create', 'update', 'retire', 'source_status')),
    scope text NOT NULL,
    organization_id text NOT NULL,
    project_id text,
    role_id text,
    subject text NOT NULL,
    summary text NOT NULL,
    tags jsonb NOT NULL,
    source_kind text NOT NULL CHECK (
        source_kind IN ('document', 'work_item', 'event', 'decision', 'policy', 'release')
    ),
    source_ref text NOT NULL CHECK (
        (source_kind = 'document' AND source_ref LIKE 'document://_%')
        OR (source_kind = 'work_item' AND source_ref LIKE 'work-item://_%')
        OR (source_kind = 'event' AND source_ref LIKE 'event://_%')
        OR (source_kind = 'decision' AND source_ref LIKE 'decision://_%')
        OR (source_kind = 'policy' AND source_ref LIKE 'policy://_%')
        OR (source_kind = 'release' AND source_ref LIKE 'release://_%')
    ),
    observed_source_version text NOT NULL,
    source_state text NOT NULL CHECK (source_state IN ('current', 'stale', 'removed')),
    current_source_version text,
    status text NOT NULL CHECK (status IN ('active', 'retired')),
    created_by text NOT NULL,
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (memory_id, version),
    UNIQUE (organization_id, operation_id),
    FOREIGN KEY (memory_id) REFERENCES agentic_mesh_v5.shared_memory_entries(memory_id)
        ON DELETE RESTRICT,
    CHECK (
        (source_state = 'removed' AND current_source_version IS NULL)
        OR (source_state IN ('current', 'stale') AND current_source_version IS NOT NULL)
    )
);

CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_shared_memory_revision_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'shared memory revisions are immutable';
END;
$$;

CREATE TRIGGER shared_memory_revisions_immutable
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.shared_memory_revisions
    FOR EACH ROW
    EXECUTE FUNCTION agentic_mesh_v5.reject_shared_memory_revision_mutation();
