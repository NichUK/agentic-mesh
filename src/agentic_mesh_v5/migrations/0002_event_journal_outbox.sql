DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agentic_mesh_v5.events) THEN
        RAISE EXCEPTION
            'migration 0002 requires an empty v1 event journal; archive or remove unsupported pre-writer rows';
    END IF;
END;
$$;

ALTER TABLE agentic_mesh_v5.events
    DROP CONSTRAINT events_project_id_fkey,
    ADD CONSTRAINT events_project_fk
        FOREIGN KEY (project_id)
        REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE RESTRICT;

ALTER TABLE agentic_mesh_v5.events
    ADD COLUMN work_item_id text NOT NULL,
    ADD COLUMN actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    ADD COLUMN correlation_id text NOT NULL CHECK (btrim(correlation_id) <> ''),
    ADD COLUMN causation_id text,
    ADD CONSTRAINT events_work_item_fk
        FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE RESTRICT;

CREATE INDEX events_correlation_idx
    ON agentic_mesh_v5.events(project_id, correlation_id, event_id);

CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'event journal records are append-only';
END;
$$;

CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.events
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_event_mutation();
