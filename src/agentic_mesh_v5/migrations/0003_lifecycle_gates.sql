CREATE TABLE agentic_mesh_v5.project_sponsors (
    project_id text NOT NULL,
    sponsor_id text NOT NULL CHECK (btrim(sponsor_id) <> ''),
    added_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, sponsor_id),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE
);

ALTER TABLE agentic_mesh_v5.work_items
    ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    ADD COLUMN terminal_reason text,
    ADD COLUMN terminal_evidence jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE agentic_mesh_v5.gates
    ADD COLUMN correlation_id text,
    ADD COLUMN evidence jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE agentic_mesh_v5.gates
SET correlation_id = 'legacy-gate:' || gate_id
WHERE correlation_id IS NULL;

ALTER TABLE agentic_mesh_v5.gates
    ALTER COLUMN correlation_id SET NOT NULL,
    ADD CONSTRAINT gates_correlation_id_check
        CHECK (btrim(correlation_id) <> ''),
    ADD CONSTRAINT gates_status_check
        CHECK (status IN ('pending', 'approved', 'rejected'));

ALTER TABLE agentic_mesh_v5.approvals
    ADD COLUMN evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD CONSTRAINT approvals_one_request_per_sponsor
        UNIQUE (project_id, gate_id, approver_id);

CREATE INDEX gates_pending_work_idx
    ON agentic_mesh_v5.gates(project_id, work_item_id, requested_at)
    WHERE status = 'pending';
