CREATE TABLE agentic_mesh_v5.work_item_ado_links (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    manifest_digest text NOT NULL,
    organization_url text NOT NULL CHECK (btrim(organization_url) <> ''),
    ado_project text NOT NULL CHECK (btrim(ado_project) <> ''),
    external_work_item_id bigint NOT NULL CHECK (external_work_item_id > 0),
    external_url text NOT NULL CHECK (btrim(external_url) <> ''),
    linked_by text NOT NULL CHECK (btrim(linked_by) <> ''),
    linked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id),
    UNIQUE (organization_url, ado_project, external_work_item_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(
            project_id, manifest_digest
        ) ON DELETE RESTRICT
);

CREATE TABLE agentic_mesh_v5.work_item_ado_update_operations (
    project_id text NOT NULL,
    operation_id text NOT NULL CHECK (btrim(operation_id) <> ''),
    work_item_id text NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    requested_fields jsonb NOT NULL CHECK (
        jsonb_typeof(requested_fields) = 'object'
        AND requested_fields <> '{}'::jsonb
    ),
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'succeeded')
    ),
    external_revision integer CHECK (external_revision > 0),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error text,
    started_by text NOT NULL CHECK (btrim(started_by) <> ''),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (project_id, operation_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_item_ado_links(project_id, work_item_id)
        ON DELETE CASCADE,
    CHECK (
        (status = 'pending' AND completed_at IS NULL)
        OR (
            status = 'succeeded'
            AND external_revision IS NOT NULL
            AND completed_at IS NOT NULL
            AND last_error IS NULL
        )
    )
);

CREATE INDEX work_item_ado_operations_pending_idx
    ON agentic_mesh_v5.work_item_ado_update_operations(project_id, status);
