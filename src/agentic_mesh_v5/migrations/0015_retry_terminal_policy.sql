CREATE TABLE agentic_mesh_v5.failure_incidents (
    project_id text NOT NULL,
    incident_id text NOT NULL CHECK (btrim(incident_id) <> ''),
    work_item_id text NOT NULL,
    owner_role_id text NOT NULL,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    failure_category text NOT NULL CHECK (btrim(failure_category) <> ''),
    safe_summary text NOT NULL CHECK (
        btrim(safe_summary) <> '' AND char_length(safe_summary) <= 1000
    ),
    source_ref text NOT NULL CHECK (
        btrim(source_ref) <> '' AND char_length(source_ref) <= 1000
    ),
    status text NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'recovered', 'terminal_eligible', 'terminal')
    ),
    next_stage text NOT NULL DEFAULT 'technical' CHECK (
        next_stage IN ('technical', 'pm_correction', 'recovery', 'none')
    ),
    next_attempt_number integer CHECK (next_attempt_number BETWEEN 1 AND 3),
    started_by text NOT NULL CHECK (btrim(started_by) <> ''),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    PRIMARY KEY (project_id, incident_id),
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, owner_role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id),
    CHECK (
        (status = 'active' AND next_stage <> 'none'
            AND next_attempt_number IS NOT NULL AND resolved_at IS NULL)
        OR (status = 'terminal_eligible' AND next_stage = 'none'
            AND next_attempt_number IS NULL AND resolved_at IS NULL)
        OR (status IN ('recovered', 'terminal') AND next_stage = 'none'
            AND next_attempt_number IS NULL AND resolved_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX failure_incidents_one_open_idx
    ON agentic_mesh_v5.failure_incidents(project_id, work_item_id)
    WHERE status IN ('active', 'terminal_eligible');

CREATE TABLE agentic_mesh_v5.failure_attempts (
    project_id text NOT NULL,
    attempt_id text NOT NULL CHECK (btrim(attempt_id) <> ''),
    incident_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 7),
    stage text NOT NULL CHECK (stage IN ('technical', 'pm_correction', 'recovery')),
    stage_attempt integer NOT NULL CHECK (stage_attempt BETWEEN 1 AND 3),
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    correction_instruction text,
    correction_digest text CHECK (
        correction_digest IS NULL OR correction_digest ~ '^[0-9a-f]{64}$'
    ),
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, attempt_id),
    UNIQUE (project_id, incident_id, ordinal),
    UNIQUE (project_id, incident_id, stage, stage_attempt),
    FOREIGN KEY (project_id, incident_id)
        REFERENCES agentic_mesh_v5.failure_incidents(project_id, incident_id)
        ON DELETE CASCADE,
    CHECK (
        (stage = 'pm_correction'
            AND correction_instruction IS NOT NULL
            AND btrim(correction_instruction) <> ''
            AND char_length(correction_instruction) <= 2000
            AND correction_digest IS NOT NULL)
        OR (stage <> 'pm_correction'
            AND correction_instruction IS NULL
            AND correction_digest IS NULL)
    ),
    CHECK ((stage = 'technical' AND stage_attempt BETWEEN 1 AND 3)
        OR (stage = 'pm_correction' AND stage_attempt BETWEEN 1 AND 3)
        OR (stage = 'recovery' AND stage_attempt = 1))
);

CREATE UNIQUE INDEX failure_attempts_distinct_correction_idx
    ON agentic_mesh_v5.failure_attempts(project_id, incident_id, correction_digest)
    WHERE correction_digest IS NOT NULL;

CREATE FUNCTION agentic_mesh_v5.reject_failure_attempt_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'failure attempt history is immutable';
END;
$$;

CREATE TRIGGER failure_attempts_immutable
    BEFORE UPDATE OR DELETE ON agentic_mesh_v5.failure_attempts
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.reject_failure_attempt_mutation();

CREATE TABLE agentic_mesh_v5.recovery_requests (
    project_id text NOT NULL,
    recovery_request_id text NOT NULL CHECK (btrim(recovery_request_id) <> ''),
    incident_id text NOT NULL,
    work_item_id text NOT NULL,
    exact_goal text NOT NULL CHECK (
        btrim(exact_goal) <> '' AND char_length(exact_goal) <= 4000
    ),
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'succeeded', 'failed')
    ),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, recovery_request_id),
    UNIQUE (project_id, incident_id),
    FOREIGN KEY (project_id, incident_id)
        REFERENCES agentic_mesh_v5.failure_incidents(project_id, incident_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE CASCADE,
    CHECK ((status = 'pending' AND resolved_at IS NULL)
        OR (status IN ('succeeded', 'failed') AND resolved_at IS NOT NULL))
);

CREATE INDEX recovery_requests_pending_idx
    ON agentic_mesh_v5.recovery_requests(requested_at, project_id, recovery_request_id)
    WHERE status = 'pending';
