CREATE TABLE agentic_mesh_v5.read_model_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL CHECK (btrim(project_id) <> ''),
    domain text NOT NULL CHECK (
        domain IN ('project', 'work', 'queue', 'role', 'instance', 'progress')
    ),
    entity_id text NOT NULL CHECK (btrim(entity_id) <> ''),
    operation text NOT NULL CHECK (operation IN ('upsert', 'delete')),
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, event_id),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE
);

CREATE INDEX read_model_events_project_order_idx
    ON agentic_mesh_v5.read_model_events(project_id, event_id);

CREATE TABLE agentic_mesh_v5.read_model_entities (
    project_id text NOT NULL CHECK (btrim(project_id) <> ''),
    domain text NOT NULL CHECK (
        domain IN ('project', 'work', 'queue', 'role', 'instance', 'progress')
    ),
    entity_id text NOT NULL,
    payload jsonb NOT NULL,
    source_event_id bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, domain, entity_id),
    FOREIGN KEY (project_id, source_event_id)
        REFERENCES agentic_mesh_v5.read_model_events(project_id, event_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE
);

CREATE TABLE agentic_mesh_v5.read_model_cursors (
    project_id text PRIMARY KEY CHECK (btrim(project_id) <> ''),
    last_event_id bigint NOT NULL CHECK (last_event_id >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION agentic_mesh_v5.emit_read_model_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source jsonb;
    selected_domain text;
    selected_entity_id text;
    selected_payload jsonb;
BEGIN
    source := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM agentic_mesh_v5.projects
        WHERE project_id = source->>'project_id'
    ) THEN
        RETURN OLD;
    END IF;
    selected_domain := CASE TG_TABLE_NAME
        WHEN 'projects' THEN 'project'
        WHEN 'work_items' THEN 'work'
        WHEN 'role_queues' THEN 'queue'
        WHEN 'queue_items' THEN 'queue'
        WHEN 'roles' THEN 'role'
        WHEN 'role_instances' THEN 'instance'
        WHEN 'progress' THEN 'progress'
        ELSE NULL
    END;
    selected_entity_id := CASE TG_TABLE_NAME
        WHEN 'projects' THEN source->>'project_id'
        WHEN 'work_items' THEN source->>'work_item_id'
        WHEN 'role_queues' THEN source->>'queue_id'
        WHEN 'queue_items' THEN source->>'queue_id'
        WHEN 'roles' THEN source->>'role_id'
        WHEN 'role_instances' THEN source->>'instance_id'
        WHEN 'progress' THEN source->>'progress_id'
        ELSE NULL
    END;
    IF selected_domain IS NULL OR selected_entity_id IS NULL THEN
        RAISE EXCEPTION 'unsupported read-model source table %', TG_TABLE_NAME;
    END IF;
    selected_payload := CASE
        WHEN TG_OP = 'DELETE' AND TG_TABLE_NAME <> 'queue_items' THEN '{}'::jsonb
        WHEN TG_TABLE_NAME = 'projects' THEN jsonb_build_object(
            'project_id', source->'project_id',
            'display_name', source->'display_name',
            'status', source->'status',
            'updated_at', source->'updated_at'
        )
        WHEN TG_TABLE_NAME = 'work_items' THEN jsonb_build_object(
            'work_item_id', source->'work_item_id',
            'parent_work_item_id', source->'parent_work_item_id',
            'assigned_role_id', source->'assigned_role_id',
            'title', source->'title',
            'status', source->'status',
            'priority', source->'priority',
            'version', source->'version',
            'terminal_reason', source->'terminal_reason',
            'terminal_evidence', source->'terminal_evidence',
            'updated_at', source->'updated_at'
        )
        WHEN TG_TABLE_NAME IN ('role_queues', 'queue_items') THEN (
            SELECT jsonb_build_object(
                'queue_id', queue.queue_id,
                'role_id', queue.role_id,
                'capability', queue.capability,
                'paused', queue.paused,
                'depth', count(item.queue_item_id) FILTER (
                    WHERE item.status IN ('ready', 'leased')
                ),
                'leased', count(item.queue_item_id) FILTER (WHERE item.status = 'leased'),
                'total_attempts', COALESCE(sum(item.attempt_count), 0)
            )
            FROM agentic_mesh_v5.role_queues AS queue
            LEFT JOIN agentic_mesh_v5.queue_items AS item
              ON item.project_id = queue.project_id
             AND item.queue_id = queue.queue_id
            WHERE queue.project_id = source->>'project_id'
              AND queue.queue_id = source->>'queue_id'
            GROUP BY queue.project_id, queue.queue_id
        )
        WHEN TG_TABLE_NAME = 'roles' THEN jsonb_build_object(
            'role_id', source->'role_id',
            'template_id', source->'template_id',
            'package_digest', source->'package_digest',
            'status', source->'status'
        )
        WHEN TG_TABLE_NAME = 'role_instances' THEN jsonb_build_object(
            'instance_id', source->'instance_id',
            'role_id', source->'role_id',
            'status', source->'status',
            'provider_ref', source->'provider_ref',
            'started_at', source->'started_at',
            'heartbeat_at', source->'heartbeat_at',
            'hibernated_at', source->'hibernated_at'
        )
        WHEN TG_TABLE_NAME = 'progress' THEN jsonb_build_object(
            'progress_id', source->'progress_id',
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
    IF selected_payload IS NULL AND TG_TABLE_NAME = 'queue_items' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    INSERT INTO agentic_mesh_v5.read_model_events
        (project_id, domain, entity_id, operation, payload)
    VALUES (
        source->>'project_id', selected_domain, selected_entity_id,
        CASE
            WHEN TG_OP = 'DELETE' AND TG_TABLE_NAME <> 'queue_items' THEN 'delete'
            ELSE 'upsert'
        END,
        jsonb_strip_nulls(selected_payload)
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

INSERT INTO agentic_mesh_v5.read_model_events
    (project_id, domain, entity_id, operation, payload)
SELECT project_id, 'project', project_id, 'upsert', jsonb_strip_nulls(
    jsonb_build_object(
        'project_id', project_id, 'display_name', display_name,
        'status', status, 'updated_at', updated_at
    )
) FROM agentic_mesh_v5.projects ORDER BY project_id;

INSERT INTO agentic_mesh_v5.read_model_events
    (project_id, domain, entity_id, operation, payload)
SELECT project_id, 'role', role_id, 'upsert', jsonb_strip_nulls(
    jsonb_build_object(
        'role_id', role_id, 'template_id', template_id,
        'package_digest', package_digest, 'status', status
    )
) FROM agentic_mesh_v5.roles ORDER BY project_id, role_id;

INSERT INTO agentic_mesh_v5.read_model_events
    (project_id, domain, entity_id, operation, payload)
SELECT project_id, 'instance', instance_id, 'upsert', jsonb_strip_nulls(
    jsonb_build_object(
        'instance_id', instance_id, 'role_id', role_id, 'status', status,
        'provider_ref', provider_ref, 'started_at', started_at,
        'heartbeat_at', heartbeat_at, 'hibernated_at', hibernated_at
    )
) FROM agentic_mesh_v5.role_instances ORDER BY project_id, instance_id;

INSERT INTO agentic_mesh_v5.read_model_events
    (project_id, domain, entity_id, operation, payload)
SELECT project_id, 'work', work_item_id, 'upsert', jsonb_strip_nulls(
    jsonb_build_object(
        'work_item_id', work_item_id, 'parent_work_item_id', parent_work_item_id,
        'assigned_role_id', assigned_role_id, 'title', title, 'status', status,
        'priority', priority, 'version', version,
        'terminal_reason', terminal_reason,
        'terminal_evidence', terminal_evidence, 'updated_at', updated_at
    )
) FROM agentic_mesh_v5.work_items ORDER BY project_id, work_item_id;

INSERT INTO agentic_mesh_v5.read_model_events
    (project_id, domain, entity_id, operation, payload)
SELECT queue.project_id, 'queue', queue.queue_id, 'upsert', jsonb_strip_nulls(
    jsonb_build_object(
        'queue_id', queue.queue_id, 'role_id', queue.role_id,
        'capability', queue.capability, 'paused', queue.paused,
        'depth', count(item.queue_item_id) FILTER (WHERE item.status IN ('ready', 'leased')),
        'leased', count(item.queue_item_id) FILTER (WHERE item.status = 'leased'),
        'total_attempts', COALESCE(sum(item.attempt_count), 0)
    )
) FROM agentic_mesh_v5.role_queues AS queue
LEFT JOIN agentic_mesh_v5.queue_items AS item
  ON item.project_id = queue.project_id AND item.queue_id = queue.queue_id
GROUP BY queue.project_id, queue.queue_id
ORDER BY queue.project_id, queue.queue_id;

INSERT INTO agentic_mesh_v5.read_model_events
    (project_id, domain, entity_id, operation, payload)
SELECT project_id, 'progress', progress_id::text, 'upsert', jsonb_strip_nulls(
    jsonb_build_object(
        'progress_id', progress_id, 'work_item_id', work_item_id,
        'role_instance_id', role_instance_id, 'sequence', sequence,
        'status', status, 'goal', goal, 'step', step,
        'completed_action', completed_action, 'activity', activity,
        'blocker', blocker, 'next_action', next_action,
        'safe_summary', safe_summary, 'recorded_at', recorded_at
    )
) FROM agentic_mesh_v5.progress ORDER BY project_id, progress_id;

CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_read_model_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM agentic_mesh_v5.projects
        WHERE project_id = OLD.project_id
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'read-model events are append-only';
END;
$$;

CREATE TRIGGER read_model_events_append_only
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.read_model_events
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_read_model_event_mutation();

CREATE TRIGGER projects_read_model_event
    AFTER INSERT OR UPDATE ON agentic_mesh_v5.projects
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
CREATE TRIGGER work_items_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.work_items
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
CREATE TRIGGER role_queues_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.role_queues
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
CREATE TRIGGER queue_items_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.queue_items
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
CREATE TRIGGER roles_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.roles
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
CREATE TRIGGER role_instances_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.role_instances
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
CREATE TRIGGER progress_read_model_event
    AFTER INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.progress
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.emit_read_model_event();
