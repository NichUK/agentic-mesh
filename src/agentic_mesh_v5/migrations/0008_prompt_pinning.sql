ALTER TABLE agentic_mesh_v5.thread_affinities
    ALTER COLUMN provider_id DROP NOT NULL,
    ALTER COLUMN thread_id DROP NOT NULL,
    ADD COLUMN prompt_digest text NOT NULL DEFAULT 'unpinned',
    ADD COLUMN generation integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    ADD COLUMN affinity_state text NOT NULL DEFAULT 'active',
    ADD COLUMN active_operation_id text,
    ADD COLUMN active_instance_id text,
    ADD COLUMN active_started_at timestamptz,
    ADD COLUMN pending_reseed_id text,
    ADD CONSTRAINT thread_affinities_prompt_digest_check CHECK (
        prompt_digest = 'unpinned' OR prompt_digest ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT thread_affinities_state_check CHECK (
        (
            affinity_state = 'active'
            AND provider_id IS NOT NULL
            AND thread_id IS NOT NULL
            AND pending_reseed_id IS NULL
        ) OR (
            affinity_state = 'pending_seed'
            AND provider_id IS NULL
            AND thread_id IS NULL
            AND pending_reseed_id IS NOT NULL
            AND prompt_digest <> 'unpinned'
        )
    ),
    ADD CONSTRAINT thread_affinities_active_operation_check CHECK (
        (
            active_operation_id IS NULL
            AND active_instance_id IS NULL
            AND active_started_at IS NULL
        ) OR (
            active_operation_id IS NOT NULL
            AND active_instance_id IS NOT NULL
            AND active_started_at IS NOT NULL
            AND affinity_state = 'active'
        )
    ),
    ADD CONSTRAINT thread_affinities_active_instance_fk
        FOREIGN KEY (project_id, active_instance_id, role_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id, role_id);

ALTER TABLE agentic_mesh_v5.thread_affinities
    ALTER COLUMN prompt_digest DROP DEFAULT;

CREATE TABLE agentic_mesh_v5.thread_reseeds (
    project_id text NOT NULL,
    reseed_id text NOT NULL CHECK (btrim(reseed_id) <> ''),
    work_item_id text NOT NULL,
    role_id text NOT NULL,
    conversation_id text NOT NULL,
    generation integer NOT NULL CHECK (generation > 1),
    old_provider_id text NOT NULL CHECK (btrim(old_provider_id) <> ''),
    old_thread_id text NOT NULL CHECK (btrim(old_thread_id) <> ''),
    old_prompt_digest text NOT NULL CHECK (
        old_prompt_digest = 'unpinned'
        OR old_prompt_digest ~ '^[0-9a-f]{64}$'
    ),
    new_prompt_digest text NOT NULL CHECK (new_prompt_digest ~ '^[0-9a-f]{64}$'),
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, reseed_id),
    UNIQUE (project_id, work_item_id, role_id, conversation_id, generation),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (project_id, role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id)
);

ALTER TABLE agentic_mesh_v5.thread_affinities
    ADD CONSTRAINT thread_affinities_pending_reseed_fk
        FOREIGN KEY (project_id, pending_reseed_id)
        REFERENCES agentic_mesh_v5.thread_reseeds(project_id, reseed_id);

CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_thread_reseed_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'thread reseed records are append-only';
END;
$$;

CREATE TRIGGER thread_reseeds_append_only
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.thread_reseeds
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_thread_reseed_mutation();
