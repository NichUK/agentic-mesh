CREATE TABLE agentic_mesh_v5.project_runtime_boundaries (
    project_id text PRIMARY KEY,
    manifest_digest text NOT NULL,
    configuration_revision text NOT NULL
        CHECK (configuration_revision ~ '^[0-9a-f]{40}$'),
    configuration_root text NOT NULL CHECK (btrim(configuration_root) <> ''),
    running_image_ref text NOT NULL CHECK (
        running_image_ref ~ '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'
    ),
    running_install_root text NOT NULL CHECK (btrim(running_install_root) <> ''),
    runtime_state_root text NOT NULL CHECK (btrim(runtime_state_root) <> ''),
    workspace_root text NOT NULL CHECK (btrim(workspace_root) <> ''),
    deployment_adapter text NOT NULL CHECK (
        deployment_adapter ~ '^[a-z][a-z0-9-]{0,127}$'
    ),
    registered_by text NOT NULL CHECK (btrim(registered_by) <> ''),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (configuration_root <> running_install_root
       AND configuration_root <> runtime_state_root
       AND configuration_root <> workspace_root
       AND running_install_root <> runtime_state_root
       AND running_install_root <> workspace_root
       AND runtime_state_root <> workspace_root),
    UNIQUE (running_install_root),
    UNIQUE (runtime_state_root),
    UNIQUE (workspace_root),
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(
            project_id, manifest_digest
        ) ON DELETE RESTRICT
);
