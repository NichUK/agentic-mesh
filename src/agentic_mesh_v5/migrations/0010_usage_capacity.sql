CREATE TABLE agentic_mesh_v5.turn_usage (
    project_id text NOT NULL,
    provider_id text NOT NULL CHECK (
        btrim(provider_id) <> '' AND char_length(provider_id) <= 128
    ),
    account_scope text NOT NULL CHECK (
        btrim(account_scope) <> '' AND char_length(account_scope) <= 128
    ),
    turn_id text NOT NULL CHECK (
        btrim(turn_id) <> '' AND char_length(turn_id) <= 128
    ),
    work_item_id text NOT NULL,
    role_instance_id text NOT NULL,
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens bigint NOT NULL CHECK (cached_input_tokens >= 0),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
    reasoning_output_tokens bigint NOT NULL CHECK (reasoning_output_tokens >= 0),
    total_tokens bigint NOT NULL CHECK (total_tokens >= 0),
    first_observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, provider_id, account_scope, turn_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, role_instance_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id)
);

CREATE INDEX turn_usage_project_time_idx
    ON agentic_mesh_v5.turn_usage(project_id, updated_at DESC);

CREATE TABLE agentic_mesh_v5.provider_capacity (
    project_id text NOT NULL,
    provider_id text NOT NULL CHECK (
        btrim(provider_id) <> '' AND char_length(provider_id) <= 128
    ),
    account_scope text NOT NULL CHECK (
        btrim(account_scope) <> '' AND char_length(account_scope) <= 128
    ),
    observation_id text NOT NULL CHECK (
        btrim(observation_id) <> '' AND char_length(observation_id) <= 128
    ),
    available boolean NOT NULL,
    observed_at timestamptz NOT NULL,
    limit_id text,
    limit_name text,
    plan_type text,
    primary_used_percent integer CHECK (
        primary_used_percent BETWEEN 0 AND 100
    ),
    primary_resets_at timestamptz,
    primary_window_minutes integer CHECK (primary_window_minutes > 0),
    secondary_used_percent integer CHECK (
        secondary_used_percent BETWEEN 0 AND 100
    ),
    secondary_resets_at timestamptz,
    secondary_window_minutes integer CHECK (secondary_window_minutes > 0),
    credit_balance numeric,
    has_credits boolean,
    unlimited_credits boolean,
    individual_limit numeric,
    individual_used numeric,
    individual_remaining_percent integer CHECK (
        individual_remaining_percent BETWEEN 0 AND 100
    ),
    individual_resets_at timestamptz,
    reset_credits_available integer CHECK (reset_credits_available >= 0),
    reset_credits_earliest_expiry timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, provider_id, account_scope),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE,
    CHECK (available OR (
        limit_id IS NULL AND limit_name IS NULL AND plan_type IS NULL
        AND primary_used_percent IS NULL AND primary_resets_at IS NULL
        AND primary_window_minutes IS NULL
        AND secondary_used_percent IS NULL AND secondary_resets_at IS NULL
        AND secondary_window_minutes IS NULL
        AND credit_balance IS NULL AND has_credits IS NULL
        AND unlimited_credits IS NULL AND individual_limit IS NULL
        AND individual_used IS NULL AND individual_remaining_percent IS NULL
        AND individual_resets_at IS NULL AND reset_credits_available IS NULL
        AND reset_credits_earliest_expiry IS NULL
    ))
);

CREATE UNIQUE INDEX provider_capacity_observation_idx
    ON agentic_mesh_v5.provider_capacity(project_id, observation_id);
