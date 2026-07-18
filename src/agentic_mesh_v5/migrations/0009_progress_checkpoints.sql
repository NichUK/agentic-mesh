ALTER TABLE agentic_mesh_v5.progress
    ADD COLUMN checkpoint_id text;

UPDATE agentic_mesh_v5.progress
SET checkpoint_id = 'legacy-' || progress_id::text
WHERE checkpoint_id IS NULL;

ALTER TABLE agentic_mesh_v5.progress
    ALTER COLUMN checkpoint_id SET NOT NULL,
    ADD CONSTRAINT progress_checkpoint_id_check CHECK (
        btrim(checkpoint_id) <> '' AND char_length(checkpoint_id) <= 128
    ),
    ADD CONSTRAINT progress_project_checkpoint_key
        UNIQUE (project_id, checkpoint_id);

CREATE INDEX progress_work_latest_idx
    ON agentic_mesh_v5.progress(project_id, work_item_id, sequence DESC);
