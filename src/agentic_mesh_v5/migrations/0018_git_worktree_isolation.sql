CREATE TABLE agentic_mesh_v5.git_workspaces (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    manifest_digest text NOT NULL,
    workspace_root text NOT NULL CHECK (btrim(workspace_root) <> ''),
    status text NOT NULL CHECK (
        status IN ('preparing', 'ready', 'releasing', 'cleanup_blocked', 'released', 'error')
    ),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    released_at timestamptz,
    PRIMARY KEY (project_id, work_item_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(project_id, manifest_digest)
        ON DELETE RESTRICT
);

CREATE TABLE agentic_mesh_v5.git_workspace_repositories (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    repository_id text NOT NULL CHECK (btrim(repository_id) <> ''),
    repository_url text NOT NULL CHECK (btrim(repository_url) <> ''),
    default_branch text NOT NULL CHECK (btrim(default_branch) <> ''),
    source_path text NOT NULL CHECK (btrim(source_path) <> ''),
    worktree_path text NOT NULL CHECK (btrim(worktree_path) <> ''),
    branch_name text NOT NULL CHECK (btrim(branch_name) <> ''),
    base_revision text NOT NULL CHECK (base_revision ~ '^[0-9a-f]{40}$'),
    current_revision text CHECK (current_revision ~ '^[0-9a-f]{40}$'),
    status text NOT NULL CHECK (
        status IN ('planned', 'ready', 'cleanup_blocked', 'released', 'error')
    ),
    last_error text,
    prepared_at timestamptz,
    released_at timestamptz,
    PRIMARY KEY (project_id, work_item_id, repository_id),
    UNIQUE (worktree_path),
    UNIQUE (source_path, branch_name),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.git_workspaces(project_id, work_item_id)
        ON DELETE RESTRICT
);

CREATE INDEX git_workspaces_status_idx
    ON agentic_mesh_v5.git_workspaces(status, updated_at);
