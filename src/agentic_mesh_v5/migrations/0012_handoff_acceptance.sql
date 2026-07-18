ALTER TABLE agentic_mesh_v5.handoffs
    ADD COLUMN source_queue_item_id text,
    ADD COLUMN source_lease_id text,
    ADD COLUMN queue_item_id text,
    ADD COLUMN target_lease_id text,
    ADD COLUMN queued_at timestamptz,
    ADD COLUMN request_fingerprint text;

UPDATE agentic_mesh_v5.handoffs
SET queued_at = offered_at
WHERE queued_at IS NULL;

ALTER TABLE agentic_mesh_v5.handoffs
    ALTER COLUMN queued_at SET NOT NULL,
    ADD CONSTRAINT handoffs_status_check
        CHECK (status IN ('offered', 'claimed', 'accepted')),
    ADD CONSTRAINT handoffs_request_fingerprint_check
        CHECK (
            request_fingerprint IS NULL
            OR request_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    ADD CONSTRAINT handoffs_timestamp_order_check
        CHECK (
            queued_at >= offered_at
            AND (claimed_at IS NULL OR claimed_at >= queued_at)
            AND (accepted_at IS NULL OR accepted_at >= claimed_at)
        ),
    ADD CONSTRAINT handoffs_state_shape_check
        CHECK (
            (status = 'offered' AND claimed_at IS NULL AND accepted_at IS NULL)
            OR (status = 'claimed' AND claimed_at IS NOT NULL AND accepted_at IS NULL)
            OR (status = 'accepted' AND claimed_at IS NOT NULL AND accepted_at IS NOT NULL)
        ),
    ADD CONSTRAINT handoffs_source_item_fk
        FOREIGN KEY (project_id, source_queue_item_id)
        REFERENCES agentic_mesh_v5.queue_items(project_id, queue_item_id),
    ADD CONSTRAINT handoffs_source_lease_fk
        FOREIGN KEY (project_id, source_lease_id)
        REFERENCES agentic_mesh_v5.leases(project_id, lease_id),
    ADD CONSTRAINT handoffs_target_item_fk
        FOREIGN KEY (project_id, queue_item_id)
        REFERENCES agentic_mesh_v5.queue_items(project_id, queue_item_id),
    ADD CONSTRAINT handoffs_target_lease_fk
        FOREIGN KEY (project_id, target_lease_id)
        REFERENCES agentic_mesh_v5.leases(project_id, lease_id);

CREATE UNIQUE INDEX handoffs_target_item_key
    ON agentic_mesh_v5.handoffs(project_id, queue_item_id)
    WHERE queue_item_id IS NOT NULL;

CREATE INDEX handoffs_source_completion_idx
    ON agentic_mesh_v5.handoffs(project_id, source_queue_item_id, status)
    WHERE status <> 'accepted';
