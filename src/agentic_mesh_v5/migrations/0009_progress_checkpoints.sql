ALTER TABLE agentic_mesh_v5.progress
    ADD COLUMN checkpoint_id text;

CREATE FUNCTION agentic_mesh_v5.emit_progress_checkpoint_read_model_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source jsonb;
    selected_payload jsonb;
BEGIN
    source := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM agentic_mesh_v5.projects
        WHERE project_id = source->>'project_id'
    ) THEN
        RETURN OLD;
    END IF;
    selected_payload := CASE
        WHEN TG_OP = 'DELETE' THEN '{}'::jsonb
        ELSE jsonb_build_object(
            'progress_id', source->'progress_id',
            'checkpoint_id', source->'checkpoint_id',
            'work_item_id', source->'work_item_id',
            'role_instance_id', source->'role_instance_id',
            'sequence', source->'sequence',
            'status', source->'status',
            'goal', source->'goal',
            'step', source->'step',
            'completed_action', source->'completed_action',
            'activity', source->'activity',
            'blocker', source->'blocker',
            'next_action', source->'next_action',
            'safe_summary', source->'safe_summary',
            'recorded_at', source->'recorded_at'
        )
    END;
    INSERT INTO agentic_mesh_v5.read_model_events
        (project_id, domain, entity_id, operation, payload)
    VALUES (
        source->>'project_id', 'progress', source->>'progress_id',
        CASE WHEN TG_OP = 'DELETE' THEN 'delete' ELSE 'upsert' END,
        jsonb_strip_nulls(selected_payload)
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER progress_read_model_event ON agentic_mesh_v5.progress;

CREATE TRIGGER progress_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.progress
    FOR EACH ROW EXECUTE FUNCTION
        agentic_mesh_v5.emit_progress_checkpoint_read_model_event();

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
