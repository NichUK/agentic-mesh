CREATE TABLE agentic_mesh_v5.project_import_previews (
    import_id uuid NOT NULL REFERENCES
        agentic_mesh_v5.project_import_question_sessions(import_id)
        ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    question_version integer NOT NULL CHECK (question_version > 0),
    resolution_digest text NOT NULL CHECK (
        resolution_digest ~ '^[0-9a-f]{64}$'
    ),
    preview_digest text NOT NULL CHECK (preview_digest ~ '^[0-9a-f]{64}$'),
    project_id text NOT NULL CHECK (btrim(project_id) <> ''),
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    manifest_snapshot jsonb NOT NULL CHECK (
        jsonb_typeof(manifest_snapshot) = 'object'
    ),
    source_selections jsonb NOT NULL CHECK (
        jsonb_typeof(source_selections) = 'array'
    ),
    backlog_candidates jsonb NOT NULL CHECK (
        jsonb_typeof(backlog_candidates) = 'array'
    ),
    selected_candidate_ids jsonb NOT NULL CHECK (
        jsonb_typeof(selected_candidate_ids) = 'array'
    ),
    status text NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'superseded', 'approved', 'rejected', 'activated')
    ),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (import_id, revision),
    UNIQUE (import_id, preview_digest)
);

CREATE TABLE agentic_mesh_v5.project_import_preview_decisions (
    import_id uuid NOT NULL,
    revision integer NOT NULL,
    sponsor_id text NOT NULL CHECK (btrim(sponsor_id) <> ''),
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    rationale text NOT NULL CHECK (btrim(rationale) <> ''),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (import_id, revision),
    FOREIGN KEY (import_id, revision) REFERENCES
        agentic_mesh_v5.project_import_previews(import_id, revision)
        ON DELETE CASCADE
);

CREATE TABLE agentic_mesh_v5.project_import_activations (
    import_id uuid PRIMARY KEY,
    revision integer NOT NULL,
    operation_id text NOT NULL CHECK (btrim(operation_id) <> ''),
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'activated')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    receipt jsonb,
    last_error text,
    requested_by text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    activated_at timestamptz,
    FOREIGN KEY (import_id, revision) REFERENCES
        agentic_mesh_v5.project_import_previews(import_id, revision)
        ON DELETE CASCADE,
    CHECK (
        (status = 'pending' AND receipt IS NULL AND activated_at IS NULL)
        OR (status = 'activated' AND receipt IS NOT NULL
            AND activated_at IS NOT NULL AND last_error IS NULL)
    )
);

CREATE INDEX project_import_previews_status_idx
    ON agentic_mesh_v5.project_import_previews(status, created_at);
