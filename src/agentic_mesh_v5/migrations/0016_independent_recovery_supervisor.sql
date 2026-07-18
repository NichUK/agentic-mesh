CREATE OR REPLACE FUNCTION agentic_mesh_v5.protect_recovery_request_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.recovery_request_id IS DISTINCT FROM OLD.recovery_request_id
       OR NEW.incident_id IS DISTINCT FROM OLD.incident_id
       OR NEW.work_item_id IS DISTINCT FROM OLD.work_item_id
       OR NEW.exact_goal IS DISTINCT FROM OLD.exact_goal
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'recovery request identity and exact goal are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER recovery_request_identity_immutable
    BEFORE UPDATE ON agentic_mesh_v5.recovery_requests
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.protect_recovery_request_identity();

CREATE TABLE agentic_mesh_v5.recovery_supervisor_runs (
    project_id text NOT NULL,
    recovery_request_id text NOT NULL,
    run_id text NOT NULL CHECK (btrim(run_id) <> ''),
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'reported')),
    claim_count integer NOT NULL DEFAULT 1 CHECK (claim_count > 0),
    owner_id text NOT NULL CHECK (btrim(owner_id) <> ''),
    lease_token text NOT NULL UNIQUE CHECK (btrim(lease_token) <> ''),
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_expires_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    time_limit_minutes integer NOT NULL CHECK (time_limit_minutes > 0),
    usage_limit bigint NOT NULL CHECK (usage_limit > 0),
    tool_profile_reference text NOT NULL CHECK (btrim(tool_profile_reference) <> ''),
    tool_profile_digest text NOT NULL CHECK (tool_profile_digest ~ '^[0-9a-f]{64}$'),
    outcome text CHECK (outcome IN ('succeeded', 'failed')),
    safe_summary text CHECK (
        safe_summary IS NULL OR (btrim(safe_summary) <> '' AND char_length(safe_summary) <= 1000)
    ),
    verification_ref text CHECK (
        verification_ref IS NULL OR (btrim(verification_ref) <> '' AND char_length(verification_ref) <= 1000)
    ),
    usage_used bigint CHECK (usage_used IS NULL OR usage_used >= 0),
    result_fingerprint text CHECK (
        result_fingerprint IS NULL OR result_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    reported_at timestamptz,
    result_applied_at timestamptz,
    PRIMARY KEY (project_id, recovery_request_id),
    UNIQUE (project_id, run_id),
    FOREIGN KEY (project_id, recovery_request_id)
        REFERENCES agentic_mesh_v5.recovery_requests(project_id, recovery_request_id)
        ON DELETE CASCADE,
    CHECK (lease_expires_at <= deadline_at),
    CHECK ((status = 'running'
            AND outcome IS NULL
            AND safe_summary IS NULL
            AND verification_ref IS NULL
            AND usage_used IS NULL
            AND result_fingerprint IS NULL
            AND reported_at IS NULL
            AND result_applied_at IS NULL)
        OR (status = 'reported'
            AND outcome IS NOT NULL
            AND safe_summary IS NOT NULL
            AND verification_ref IS NOT NULL
            AND usage_used IS NOT NULL
            AND result_fingerprint IS NOT NULL
            AND reported_at IS NOT NULL)),
    CHECK (result_applied_at IS NULL OR status = 'reported')
);

CREATE INDEX recovery_supervisor_claim_idx
    ON agentic_mesh_v5.recovery_supervisor_runs(lease_expires_at, project_id)
    WHERE status = 'running';

CREATE INDEX recovery_supervisor_report_idx
    ON agentic_mesh_v5.recovery_supervisor_runs(reported_at, project_id)
    WHERE status = 'reported' AND result_applied_at IS NULL;

CREATE OR REPLACE FUNCTION agentic_mesh_v5.require_supervised_recovery_attempt()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.stage = 'recovery' AND NOT EXISTS (
        SELECT 1
        FROM agentic_mesh_v5.recovery_supervisor_runs AS run
        JOIN agentic_mesh_v5.recovery_requests AS request
          ON request.project_id = run.project_id
         AND request.recovery_request_id = run.recovery_request_id
        WHERE run.project_id = NEW.project_id
          AND run.run_id = NEW.attempt_id
          AND request.incident_id = NEW.incident_id
          AND run.status = 'reported'
          AND run.outcome = NEW.outcome
          AND NEW.evidence = jsonb_build_object(
              'recovery_run_id', run.run_id,
              'safe_summary', run.safe_summary,
              'verification_ref', run.verification_ref,
              'usage_used', run.usage_used,
              'usage_limit', run.usage_limit,
              'tool_profile_reference', run.tool_profile_reference,
              'tool_profile_digest', run.tool_profile_digest
          )
    ) THEN
        RAISE EXCEPTION 'recovery attempt requires a verified supervisor result';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER failure_attempt_requires_recovery_supervisor
    BEFORE INSERT ON agentic_mesh_v5.failure_attempts
    FOR EACH ROW EXECUTE FUNCTION agentic_mesh_v5.require_supervised_recovery_attempt();
