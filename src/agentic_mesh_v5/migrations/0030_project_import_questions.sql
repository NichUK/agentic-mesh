CREATE TABLE agentic_mesh_v5.project_import_question_sessions (
    import_id uuid PRIMARY KEY,
    discovery_digest text NOT NULL CHECK (
        discovery_digest ~ '^[0-9a-f]{64}$'
    ),
    discovery_report jsonb NOT NULL CHECK (
        jsonb_typeof(discovery_report) = 'object'
    ),
    questionnaire_digest text NOT NULL CHECK (
        questionnaire_digest ~ '^[0-9a-f]{64}$'
    ),
    questions jsonb NOT NULL CHECK (
        jsonb_typeof(questions) = 'array'
        AND jsonb_array_length(questions) > 0
    ),
    answers jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(answers) = 'object'
    ),
    status text NOT NULL DEFAULT 'questioning' CHECK (
        status IN ('questioning', 'ready')
    ),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ready_at timestamptz,
    CHECK (
        (status = 'questioning' AND ready_at IS NULL)
        OR (status = 'ready' AND ready_at IS NOT NULL)
    )
);

CREATE TABLE agentic_mesh_v5.project_import_question_events (
    import_id uuid NOT NULL REFERENCES
        agentic_mesh_v5.project_import_question_sessions(import_id)
        ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence > 1),
    event_kind text NOT NULL CHECK (
        event_kind IN ('questions-added', 'answers-recorded')
    ),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    rationale text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (import_id, sequence),
    CHECK (event_kind <> 'questions-added' OR rationale IS NOT NULL)
);

CREATE INDEX project_import_question_sessions_status_idx
    ON agentic_mesh_v5.project_import_question_sessions(status, updated_at);
