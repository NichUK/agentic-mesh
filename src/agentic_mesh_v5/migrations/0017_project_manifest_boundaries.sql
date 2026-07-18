CREATE TABLE agentic_mesh_v5.project_manifest_snapshots (
    project_id text NOT NULL,
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    source_revision text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    source_path text NOT NULL CHECK (source_path = 'agentic-mesh/project.yaml'),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    registered_by text NOT NULL CHECK (btrim(registered_by) <> ''),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, manifest_digest),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE RESTRICT
);
CREATE TABLE agentic_mesh_v5.project_manifest_resource_claims (
    project_id text NOT NULL,
    manifest_digest text NOT NULL,
    resource_kind text NOT NULL CHECK (btrim(resource_kind) <> ''),
    resource_key text NOT NULL CHECK (btrim(resource_key) <> ''),
    owner_project_id text NOT NULL CHECK (btrim(owner_project_id) <> ''),
    authorization_ref text,
    PRIMARY KEY (project_id, manifest_digest, resource_kind, resource_key),
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(project_id, manifest_digest)
        ON DELETE RESTRICT,
    CHECK (authorization_ref IS NULL OR authorization_ref ~ '^grant://')
);
CREATE INDEX project_manifest_resource_lookup_idx
    ON agentic_mesh_v5.project_manifest_resource_claims(resource_kind, resource_key);
CREATE TABLE agentic_mesh_v5.project_manifest_active (
    project_id text PRIMARY KEY,
    manifest_digest text NOT NULL,
    activated_by text NOT NULL CHECK (btrim(activated_by) <> ''),
    activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id, manifest_digest)
        REFERENCES agentic_mesh_v5.project_manifest_snapshots(project_id, manifest_digest)
        ON DELETE RESTRICT
);
CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_project_manifest_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'project manifest snapshots and resource claims are immutable';
END;
$$;
CREATE TRIGGER project_manifest_snapshots_immutable
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.project_manifest_snapshots
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_project_manifest_mutation();
CREATE TRIGGER project_manifest_resource_claims_immutable
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.project_manifest_resource_claims
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_project_manifest_mutation();
