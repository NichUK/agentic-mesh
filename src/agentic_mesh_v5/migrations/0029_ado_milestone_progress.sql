CREATE TABLE agentic_mesh_v5.work_item_ado_milestones (
    project_id text NOT NULL,
    milestone_id text NOT NULL CHECK (btrim(milestone_id) <> ''),
    work_item_id text NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    milestone_kind text NOT NULL CHECK (
        milestone_kind IN (
            'start', 'handoff', 'blocker', 'recovery',
            'pull-request', 'deployment', 'acceptance'
        )
    ),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    summary text NOT NULL CHECK (btrim(summary) <> ''),
    evidence jsonb NOT NULL CHECK (
        jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) > 0
    ),
    next_action text NOT NULL CHECK (btrim(next_action) <> ''),
    target_state text CHECK (target_state IN ('Active', 'Resolved', 'Closed')),
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'published', 'suppressed')
    ),
    state_disposition text CHECK (
        state_disposition IN (
            'not-requested', 'already-current', 'updated',
            'preserved-manual', 'suppressed'
        )
    ),
    comment_id bigint CHECK (comment_id > 0),
    external_revision integer CHECK (external_revision > 0),
    external_state text,
    suppression_reason text,
    started_by text NOT NULL CHECK (btrim(started_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (project_id, milestone_id),
    UNIQUE (project_id, work_item_id, sequence),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_item_ado_links(project_id, work_item_id)
        ON DELETE CASCADE,
    CHECK (
        (status = 'pending' AND published_at IS NULL
            AND state_disposition IS NULL)
        OR (status = 'published' AND published_at IS NOT NULL
            AND state_disposition IS NOT NULL AND comment_id IS NOT NULL)
        OR (status = 'suppressed' AND published_at IS NOT NULL
            AND state_disposition = 'suppressed'
            AND suppression_reason IS NOT NULL AND comment_id IS NULL)
    )
);

CREATE INDEX work_item_ado_milestones_order_idx
    ON agentic_mesh_v5.work_item_ado_milestones(
        project_id, work_item_id, sequence
    );
