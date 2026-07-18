CREATE TABLE agentic_mesh_v5.pm_monitor_lease (
    monitor_id text PRIMARY KEY CHECK (monitor_id = 'global-project-manager'),
    owner_id text NOT NULL CHECK (btrim(owner_id) <> ''),
    lease_token text NOT NULL UNIQUE CHECK (btrim(lease_token) <> ''),
    lease_seconds integer NOT NULL CHECK (lease_seconds BETWEEN 1 AND 300),
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    sweep_count bigint NOT NULL DEFAULT 0 CHECK (sweep_count >= 0),
    last_sweep_at timestamptz,
    CHECK (expires_at > acquired_at)
);

CREATE TABLE agentic_mesh_v5.continuation_status (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    work_version integer NOT NULL CHECK (work_version > 0),
    disposition text NOT NULL CHECK (disposition IN (
        'progressing', 'waiting_sponsor', 'sponsor_question',
        'pm_routed', 'routing_blocked'
    )),
    action_id text,
    detail text NOT NULL CHECK (btrim(detail) <> ''),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE CASCADE
);

CREATE INDEX continuation_status_attention_idx
    ON agentic_mesh_v5.continuation_status(disposition, observed_at)
    WHERE disposition IN ('sponsor_question', 'pm_routed', 'routing_blocked');
