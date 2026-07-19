ALTER TABLE agentic_mesh_v5.work_item_ado_update_operations
    ADD COLUMN expected_fields jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(expected_fields) = 'object');

ALTER TABLE agentic_mesh_v5.work_item_ado_update_operations
    DROP CONSTRAINT work_item_ado_update_operations_status_check,
    DROP CONSTRAINT work_item_ado_update_operations_check;

ALTER TABLE agentic_mesh_v5.work_item_ado_update_operations
    ADD CONSTRAINT work_item_ado_update_operations_status_check
        CHECK (status IN ('pending', 'succeeded', 'conflicted')),
    ADD CONSTRAINT work_item_ado_update_operations_terminal_check CHECK (
        (status = 'pending' AND completed_at IS NULL)
        OR (
            status = 'succeeded'
            AND external_revision IS NOT NULL
            AND completed_at IS NOT NULL
            AND last_error IS NULL
        )
        OR (
            status = 'conflicted'
            AND external_revision IS NOT NULL
            AND completed_at IS NOT NULL
            AND last_error IS NOT NULL
        )
    );
