CREATE TABLE agentic_mesh_v5.flow_runs (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    flow_id text NOT NULL CHECK (btrim(flow_id) <> ''),
    flow_digest text NOT NULL CHECK (flow_digest ~ '^[0-9a-f]{64}$'),
    flow_snapshot jsonb NOT NULL CHECK (jsonb_typeof(flow_snapshot) = 'object'),
    current_state text NOT NULL CHECK (btrim(current_state) <> ''),
    owner_role_id text NOT NULL CHECK (btrim(owner_role_id) <> ''),
    status text NOT NULL CHECK (
        status IN (
            'active', 'handoff_preparing', 'handoff_pending',
            'completion_preparing', 'completed'
        )
    ),
    fields jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(fields) = 'object'),
    pending_route_id text,
    pending_target_state text,
    pending_target_role_id text,
    pending_handoff_id text,
    pending_operation_id text,
    pending_request_digest text CHECK (
        pending_request_digest IS NULL OR pending_request_digest ~ '^[0-9a-f]{64}$'
    ),
    version integer NOT NULL CHECK (version > 0),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    updated_by text NOT NULL CHECK (btrim(updated_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, owner_role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, pending_target_role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, pending_handoff_id)
        REFERENCES agentic_mesh_v5.handoffs(project_id, handoff_id) ON DELETE RESTRICT,
    CHECK (
        (status IN ('handoff_preparing', 'handoff_pending')
         AND pending_route_id IS NOT NULL
         AND pending_target_state IS NOT NULL AND pending_target_role_id IS NOT NULL
         AND pending_operation_id IS NOT NULL AND pending_request_digest IS NOT NULL
         AND (status = 'handoff_preparing') = (pending_handoff_id IS NULL))
        OR (status NOT IN ('handoff_preparing', 'handoff_pending') AND pending_route_id IS NULL
            AND pending_target_state IS NULL AND pending_target_role_id IS NULL
            AND pending_handoff_id IS NULL
            AND (
                (status = 'completion_preparing' AND pending_operation_id IS NOT NULL
                 AND pending_request_digest IS NOT NULL)
                OR (status <> 'completion_preparing' AND pending_operation_id IS NULL
                    AND pending_request_digest IS NULL)
            ))
    )
);

CREATE TABLE agentic_mesh_v5.flow_obligations (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    state text NOT NULL,
    entry_version integer NOT NULL CHECK (entry_version > 0),
    obligation_kind text NOT NULL CHECK (
        obligation_kind IN ('artifact', 'consult', 'gate', 'inform')
    ),
    obligation_id text NOT NULL CHECK (btrim(obligation_id) <> ''),
    accountable_role_id text NOT NULL CHECK (btrim(accountable_role_id) <> ''),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    status text NOT NULL CHECK (status IN ('pending', 'dispatched', 'satisfied')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    updated_by text NOT NULL CHECK (btrim(updated_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (
        project_id, work_item_id, state, entry_version, obligation_kind, obligation_id
    ),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.flow_runs(project_id, work_item_id) ON DELETE RESTRICT,
    CHECK (
        status = 'pending'
        OR (status = 'dispatched' AND obligation_kind IN ('consult', 'inform'))
        OR (status = 'satisfied' AND obligation_kind IN ('artifact', 'gate')
            AND evidence <> '{}'::jsonb)
    )
);

CREATE TABLE agentic_mesh_v5.flow_transition_journal (
    project_id text NOT NULL,
    work_item_id text NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    operation_id text NOT NULL CHECK (btrim(operation_id) <> ''),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    action text NOT NULL CHECK (action IN ('start', 'prepare', 'pickup', 'complete')),
    from_state text,
    to_state text NOT NULL,
    route_id text,
    handoff_id text,
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id, sequence),
    UNIQUE (project_id, operation_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.flow_runs(project_id, work_item_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, handoff_id)
        REFERENCES agentic_mesh_v5.handoffs(project_id, handoff_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION agentic_mesh_v5.reject_flow_journal_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'flow transition journal is immutable';
END;
$$;

CREATE TRIGGER flow_transition_journal_immutable
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.flow_transition_journal
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_flow_journal_mutation();
