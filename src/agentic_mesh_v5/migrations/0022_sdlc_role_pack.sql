CREATE TABLE agentic_mesh_v5.role_bindings (
    project_id text NOT NULL,
    role_id text NOT NULL,
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    role_reference text NOT NULL CHECK (btrim(role_reference) <> ''),
    role_digest text NOT NULL CHECK (role_digest ~ '^[0-9a-f]{64}$'),
    role_snapshot jsonb NOT NULL CHECK (jsonb_typeof(role_snapshot) = 'object'),
    tool_profile_reference text NOT NULL CHECK (btrim(tool_profile_reference) <> ''),
    tool_profile_digest text NOT NULL CHECK (tool_profile_digest ~ '^[0-9a-f]{64}$'),
    tool_profile_id text NOT NULL CHECK (btrim(tool_profile_id) <> ''),
    flow_reference text NOT NULL CHECK (btrim(flow_reference) <> ''),
    flow_digest text NOT NULL CHECK (flow_digest ~ '^[0-9a-f]{64}$'),
    prompt_configuration_digest text NOT NULL
        CHECK (prompt_configuration_digest ~ '^[0-9a-f]{64}$'),
    role_class text NOT NULL CHECK (btrim(role_class) <> ''),
    memory_scope text NOT NULL CHECK (memory_scope = 'project-role'),
    collaboration_identity text NOT NULL CHECK (btrim(collaboration_identity) <> ''),
    minimum_instances integer NOT NULL CHECK (minimum_instances >= 0),
    maximum_instances integer NOT NULL CHECK (maximum_instances > 0),
    activated_by text NOT NULL CHECK (btrim(activated_by) <> ''),
    activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, role_id),
    FOREIGN KEY (project_id, role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(project_id, manifest_digest)
        ON DELETE RESTRICT,
    CHECK (minimum_instances <= maximum_instances)
);

CREATE INDEX role_bindings_profile_idx
    ON agentic_mesh_v5.role_bindings(tool_profile_id, project_id, role_id);
