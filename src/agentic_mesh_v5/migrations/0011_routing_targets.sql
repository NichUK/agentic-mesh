ALTER TABLE agentic_mesh_v5.role_queues
    ADD CONSTRAINT role_queues_capability_check CHECK (
        capability IS NULL OR (
            btrim(capability) <> '' AND char_length(capability) <= 128
        )
    );

CREATE UNIQUE INDEX role_queues_target_key
    ON agentic_mesh_v5.role_queues (
        project_id, role_id, COALESCE(capability, '')
    );

ALTER TABLE agentic_mesh_v5.queue_items
    ADD COLUMN route_fingerprint text,
    ADD CONSTRAINT queue_items_route_fingerprint_check CHECK (
        route_fingerprint IS NULL OR route_fingerprint ~ '^[0-9a-f]{64}$'
    );
