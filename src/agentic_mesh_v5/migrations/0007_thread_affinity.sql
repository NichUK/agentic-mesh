ALTER TABLE agentic_mesh_v5.role_instances
    ADD CONSTRAINT role_instances_project_instance_role_key
    UNIQUE (project_id, instance_id, role_id);

CREATE TABLE agentic_mesh_v5.thread_affinities (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    role_id text NOT NULL,
    conversation_id text NOT NULL CHECK (btrim(conversation_id) <> ''),
    provider_id text NOT NULL CHECK (btrim(provider_id) <> ''),
    thread_id text NOT NULL CHECK (btrim(thread_id) <> ''),
    last_instance_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_resumed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id, role_id, conversation_id),
    UNIQUE (provider_id, thread_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, last_instance_id, role_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id, role_id)
);

CREATE INDEX thread_affinities_role_idx
    ON agentic_mesh_v5.thread_affinities(project_id, role_id, updated_at);
