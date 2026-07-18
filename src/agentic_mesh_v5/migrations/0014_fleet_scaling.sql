CREATE TABLE agentic_mesh_v5.role_scaling_policies (
    project_id text NOT NULL,
    role_id text NOT NULL,
    min_warm_instances integer NOT NULL DEFAULT 0
        CHECK (min_warm_instances >= 0),
    max_instances integer NOT NULL CHECK (max_instances > 0),
    scale_after_seconds integer NOT NULL DEFAULT 60
        CHECK (scale_after_seconds BETWEEN 1 AND 3600),
    idle_grace_seconds integer NOT NULL DEFAULT 300
        CHECK (idle_grace_seconds BETWEEN 1 AND 86400),
    hibernation_enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, role_id),
    FOREIGN KEY (project_id, role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id) ON DELETE CASCADE,
    CHECK (min_warm_instances <= max_instances),
    CHECK (
        role_id <> 'project-manager'
        OR (min_warm_instances >= 1 AND hibernation_enabled = false)
    )
);

ALTER TABLE agentic_mesh_v5.role_instances
    ADD COLUMN idle_since timestamptz,
    ADD COLUMN lifecycle_action_id text,
    ADD COLUMN lifecycle_reason text,
    ADD COLUMN last_wake_at timestamptz,
    ADD COLUMN last_lifecycle_error text;

UPDATE agentic_mesh_v5.role_instances
SET status = CASE
        WHEN status = 'starting' THEN 'stopped'
        ELSE 'hibernated'
    END,
    lifecycle_reason = 'normalized legacy transition during fleet migration'
WHERE status IN ('starting', 'hibernating');

ALTER TABLE agentic_mesh_v5.role_instances
    ADD CONSTRAINT role_instances_lifecycle_action_check CHECK (
        (
            status IN ('starting', 'hibernating')
            AND lifecycle_action_id IS NOT NULL
            AND btrim(lifecycle_action_id) <> ''
            AND lifecycle_reason IS NOT NULL
            AND btrim(lifecycle_reason) <> ''
        ) OR (
            status NOT IN ('starting', 'hibernating')
            AND lifecycle_action_id IS NULL
        )
    );

CREATE INDEX role_instances_scaling_idx
    ON agentic_mesh_v5.role_instances(project_id, role_id, status, idle_since);

CREATE UNIQUE INDEX role_instances_lifecycle_action_id_idx
    ON agentic_mesh_v5.role_instances(lifecycle_action_id)
    WHERE lifecycle_action_id IS NOT NULL;
