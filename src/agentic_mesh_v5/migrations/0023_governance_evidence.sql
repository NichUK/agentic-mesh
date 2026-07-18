CREATE TABLE agentic_mesh_v5.governance_context (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    architecture_impact text CHECK (
        architecture_impact IN ('no-material', 'material', 'uncertain')
    ),
    updated_by text NOT NULL CHECK (btrim(updated_by) <> ''),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.flow_runs(project_id, work_item_id) ON DELETE RESTRICT
);

CREATE TABLE agentic_mesh_v5.governance_records (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    record_id text NOT NULL CHECK (btrim(record_id) <> ''),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (btrim(state) <> ''),
    entry_version integer NOT NULL CHECK (entry_version > 0),
    obligation_kind text NOT NULL CHECK (
        obligation_kind IN ('artifact', 'consult', 'gate', 'inform')
    ),
    obligation_id text NOT NULL CHECK (btrim(obligation_id) <> ''),
    decision text NOT NULL CHECK (
        decision IN ('verified', 'responded', 'approved', 'rejected', 'exception')
    ),
    actor_role_id text NOT NULL CHECK (btrim(actor_role_id) <> ''),
    reason text NOT NULL DEFAULT '',
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    document_path text,
    document_etag text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, record_id),
    FOREIGN KEY (project_id, work_item_id, state, entry_version,
                 obligation_kind, obligation_id)
        REFERENCES agentic_mesh_v5.flow_obligations(
            project_id, work_item_id, state, entry_version,
            obligation_kind, obligation_id
        ) ON DELETE RESTRICT,
    CHECK (decision <> 'exception' OR btrim(reason) <> ''),
    CHECK ((document_path IS NULL) = (document_etag IS NULL))
);

CREATE INDEX governance_records_work_idx
    ON agentic_mesh_v5.governance_records(project_id, work_item_id, recorded_at);

CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_governance_record_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'governance records are immutable';
END;
$$;

CREATE TRIGGER governance_records_immutable
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.governance_records
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_governance_record_mutation();
