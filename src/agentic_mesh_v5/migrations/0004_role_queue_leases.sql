ALTER TABLE agentic_mesh_v5.queue_items
    ADD CONSTRAINT queue_items_status_check
        CHECK (status IN ('ready', 'leased', 'completed'));

ALTER TABLE agentic_mesh_v5.leases
    ADD COLUMN released_reason text,
    ADD CONSTRAINT leases_expiry_after_acquired_check
        CHECK (expires_at > acquired_at),
    ADD CONSTRAINT leases_release_pair_check
        CHECK ((released_at IS NULL) = (released_reason IS NULL));

CREATE INDEX leases_expiry_reclaim_idx
    ON agentic_mesh_v5.leases(project_id, expires_at, lease_id)
    WHERE released_at IS NULL;

CREATE INDEX queue_items_metrics_idx
    ON agentic_mesh_v5.queue_items(project_id, queue_id, status, available_at);
