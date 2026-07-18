ALTER TABLE agentic_mesh_v5.gates
    DROP CONSTRAINT gates_status_check,
    ADD COLUMN flow_state text,
    ADD COLUMN flow_entry_version integer,
    ADD COLUMN flow_obligation_kind text,
    ADD COLUMN flow_obligation_id text,
    ADD COLUMN expires_at timestamptz,
    ADD COLUMN timed_out_at timestamptz,
    ADD COLUMN resolved_operation_id text,
    ADD COLUMN resolved_request_digest text,
    ADD CONSTRAINT gates_status_check
        CHECK (status IN ('pending', 'approved', 'rejected', 'timed_out'));

UPDATE agentic_mesh_v5.gates
SET resolved_operation_id = 'legacy-resolution:' || gate_id,
    resolved_request_digest = md5(project_id || ':' || gate_id || ':' || status)
                              || md5(status || ':' || gate_id || ':' || project_id)
WHERE status IN ('approved', 'rejected');

ALTER TABLE agentic_mesh_v5.gates
    ADD CONSTRAINT gates_flow_link_shape_check CHECK (
        (flow_state IS NULL AND flow_entry_version IS NULL
         AND flow_obligation_kind IS NULL AND flow_obligation_id IS NULL
         AND expires_at IS NULL)
        OR (btrim(flow_state) <> '' AND flow_entry_version > 0
            AND flow_obligation_kind = 'gate'
            AND btrim(flow_obligation_id) <> '' AND expires_at IS NOT NULL)
    ),
    ADD CONSTRAINT gates_resolution_shape_check CHECK (
        (status = 'pending' AND resolved_at IS NULL AND timed_out_at IS NULL
         AND resolved_operation_id IS NULL AND resolved_request_digest IS NULL)
        OR (status IN ('approved', 'rejected') AND resolved_at IS NOT NULL
            AND timed_out_at IS NULL
            AND (
                flow_obligation_id IS NULL
                OR (btrim(resolved_operation_id) <> ''
                    AND resolved_request_digest ~ '^[0-9a-f]{64}$')
            ))
        OR (status = 'timed_out' AND resolved_at IS NOT NULL
            AND timed_out_at IS NOT NULL AND btrim(resolved_operation_id) <> ''
            AND resolved_request_digest ~ '^[0-9a-f]{64}$')
    ),
    ADD CONSTRAINT gates_flow_obligation_fk FOREIGN KEY (
        project_id, work_item_id, flow_state, flow_entry_version,
        flow_obligation_kind, flow_obligation_id
    ) REFERENCES agentic_mesh_v5.flow_obligations(
        project_id, work_item_id, state, entry_version,
        obligation_kind, obligation_id
    ) ON DELETE RESTRICT;

CREATE UNIQUE INDEX gates_flow_obligation_key
    ON agentic_mesh_v5.gates(
        project_id, work_item_id, flow_state, flow_entry_version,
        flow_obligation_id
    ) WHERE flow_obligation_id IS NOT NULL AND status = 'pending';

CREATE UNIQUE INDEX gates_resolution_operation_key
    ON agentic_mesh_v5.gates(project_id, resolved_operation_id)
    WHERE resolved_operation_id IS NOT NULL;

CREATE INDEX gates_expiry_idx
    ON agentic_mesh_v5.gates(expires_at, project_id, gate_id)
    WHERE status = 'pending' AND expires_at IS NOT NULL;
