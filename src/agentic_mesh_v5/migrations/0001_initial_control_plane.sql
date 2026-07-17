CREATE TABLE agentic_mesh_v5.projects (
    project_id text PRIMARY KEY CHECK (btrim(project_id) <> ''),
    display_name text NOT NULL CHECK (btrim(display_name) <> ''),
    status text NOT NULL DEFAULT 'active',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE agentic_mesh_v5.roles (
    project_id text NOT NULL,
    role_id text NOT NULL CHECK (btrim(role_id) <> ''),
    template_id text NOT NULL CHECK (btrim(template_id) <> ''),
    package_digest text,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, role_id),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE
);

CREATE TABLE agentic_mesh_v5.role_instances (
    project_id text NOT NULL,
    instance_id text NOT NULL CHECK (btrim(instance_id) <> ''),
    role_id text NOT NULL,
    status text NOT NULL DEFAULT 'stopped',
    provider_ref text,
    started_at timestamptz,
    heartbeat_at timestamptz,
    hibernated_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, instance_id),
    FOREIGN KEY (project_id, role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id) ON DELETE CASCADE
);

CREATE TABLE agentic_mesh_v5.work_items (
    project_id text NOT NULL,
    work_item_id text NOT NULL CHECK (btrim(work_item_id) <> ''),
    parent_work_item_id text,
    assigned_role_id text,
    title text NOT NULL CHECK (btrim(title) <> ''),
    status text NOT NULL DEFAULT 'new',
    priority integer NOT NULL DEFAULT 0,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, work_item_id),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, parent_work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id),
    FOREIGN KEY (project_id, assigned_role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id)
);

CREATE TABLE agentic_mesh_v5.role_queues (
    project_id text NOT NULL,
    queue_id text NOT NULL CHECK (btrim(queue_id) <> ''),
    role_id text NOT NULL,
    capability text,
    paused boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, queue_id),
    FOREIGN KEY (project_id, role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id) ON DELETE CASCADE
);

CREATE TABLE agentic_mesh_v5.queue_items (
    project_id text NOT NULL,
    queue_item_id text NOT NULL CHECK (btrim(queue_item_id) <> ''),
    queue_id text NOT NULL,
    work_item_id text NOT NULL,
    status text NOT NULL DEFAULT 'ready',
    priority integer NOT NULL DEFAULT 0,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, queue_item_id),
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (project_id, queue_id)
        REFERENCES agentic_mesh_v5.role_queues(project_id, queue_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id) ON DELETE CASCADE
);

CREATE INDEX queue_items_ready_idx
    ON agentic_mesh_v5.queue_items(project_id, queue_id, status, priority DESC, available_at);

CREATE TABLE agentic_mesh_v5.leases (
    project_id text NOT NULL,
    lease_id text NOT NULL CHECK (btrim(lease_id) <> ''),
    queue_item_id text NOT NULL,
    owner_instance_id text NOT NULL,
    lease_token text NOT NULL UNIQUE CHECK (btrim(lease_token) <> ''),
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    released_at timestamptz,
    PRIMARY KEY (project_id, lease_id),
    FOREIGN KEY (project_id, queue_item_id)
        REFERENCES agentic_mesh_v5.queue_items(project_id, queue_item_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, owner_instance_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id)
);

CREATE UNIQUE INDEX leases_one_active_per_item_idx
    ON agentic_mesh_v5.leases(project_id, queue_item_id)
    WHERE released_at IS NULL;

CREATE TABLE agentic_mesh_v5.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL,
    aggregate_type text NOT NULL CHECK (btrim(aggregate_type) <> ''),
    aggregate_id text NOT NULL CHECK (btrim(aggregate_id) <> ''),
    event_type text NOT NULL CHECK (btrim(event_type) <> ''),
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, event_id),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE
);

CREATE INDEX events_stream_idx
    ON agentic_mesh_v5.events(project_id, aggregate_type, aggregate_id, event_id);

CREATE TABLE agentic_mesh_v5.outbox (
    outbox_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL,
    event_id bigint NOT NULL,
    topic text NOT NULL CHECK (btrim(topic) <> ''),
    payload jsonb NOT NULL,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    dispatched_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error text,
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (project_id, event_id)
        REFERENCES agentic_mesh_v5.events(project_id, event_id) ON DELETE CASCADE
);

CREATE INDEX outbox_pending_idx
    ON agentic_mesh_v5.outbox(available_at, outbox_id)
    WHERE dispatched_at IS NULL;

CREATE TABLE agentic_mesh_v5.handoffs (
    project_id text NOT NULL,
    handoff_id text NOT NULL CHECK (btrim(handoff_id) <> ''),
    work_item_id text NOT NULL,
    source_role_id text NOT NULL,
    source_instance_id text,
    target_role_id text NOT NULL,
    target_instance_id text,
    status text NOT NULL DEFAULT 'offered',
    summary text NOT NULL,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    offered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    accepted_at timestamptz,
    PRIMARY KEY (project_id, handoff_id),
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, source_role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id),
    FOREIGN KEY (project_id, source_instance_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id),
    FOREIGN KEY (project_id, target_role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id),
    FOREIGN KEY (project_id, target_instance_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id)
);

CREATE TABLE agentic_mesh_v5.gates (
    project_id text NOT NULL,
    gate_id text NOT NULL CHECK (btrim(gate_id) <> ''),
    work_item_id text NOT NULL,
    gate_type text NOT NULL CHECK (btrim(gate_type) <> ''),
    status text NOT NULL DEFAULT 'pending',
    requested_by text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    PRIMARY KEY (project_id, gate_id),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id) ON DELETE CASCADE
);

CREATE TABLE agentic_mesh_v5.approvals (
    project_id text NOT NULL,
    approval_id text NOT NULL CHECK (btrim(approval_id) <> ''),
    gate_id text NOT NULL,
    approver_id text NOT NULL CHECK (btrim(approver_id) <> ''),
    decision text CHECK (decision IN ('approved', 'rejected')),
    rationale text,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz,
    PRIMARY KEY (project_id, approval_id),
    FOREIGN KEY (project_id, gate_id)
        REFERENCES agentic_mesh_v5.gates(project_id, gate_id) ON DELETE CASCADE,
    CHECK ((decision IS NULL) = (decided_at IS NULL))
);

CREATE TABLE agentic_mesh_v5.memories (
    memory_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('project', 'project_role', 'organization_role')),
    project_id text,
    role_id text,
    content text NOT NULL CHECK (btrim(content) <> ''),
    sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, role_id)
        REFERENCES agentic_mesh_v5.roles(project_id, role_id),
    CHECK (
        (scope = 'project' AND project_id IS NOT NULL AND role_id IS NULL)
        OR (scope = 'project_role' AND project_id IS NOT NULL AND role_id IS NOT NULL)
        OR (scope = 'organization_role' AND project_id IS NULL AND role_id IS NOT NULL)
    )
);

CREATE TABLE agentic_mesh_v5.packages (
    package_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('organization', 'project')),
    project_id text,
    package_type text NOT NULL CHECK (btrim(package_type) <> ''),
    package_name text NOT NULL CHECK (btrim(package_name) <> ''),
    package_version text NOT NULL CHECK (btrim(package_version) <> ''),
    digest text NOT NULL CHECK (btrim(digest) <> ''),
    source_ref text NOT NULL CHECK (btrim(source_ref) <> ''),
    activated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE,
    CHECK (
        (scope = 'organization' AND project_id IS NULL)
        OR (scope = 'project' AND project_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX packages_organization_version_idx
    ON agentic_mesh_v5.packages(package_type, package_name, package_version)
    WHERE scope = 'organization';

CREATE UNIQUE INDEX packages_project_version_idx
    ON agentic_mesh_v5.packages(project_id, package_type, package_name, package_version)
    WHERE scope = 'project';

CREATE TABLE agentic_mesh_v5.progress (
    project_id text NOT NULL,
    progress_id bigint GENERATED ALWAYS AS IDENTITY,
    work_item_id text NOT NULL,
    role_instance_id text,
    sequence integer NOT NULL CHECK (sequence > 0),
    status text NOT NULL CHECK (btrim(status) <> ''),
    goal text NOT NULL,
    step text NOT NULL,
    completed_action text,
    activity text,
    blocker text,
    next_action text NOT NULL,
    safe_summary text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, progress_id),
    UNIQUE (project_id, work_item_id, sequence),
    FOREIGN KEY (project_id, work_item_id)
        REFERENCES agentic_mesh_v5.work_items(project_id, work_item_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, role_instance_id)
        REFERENCES agentic_mesh_v5.role_instances(project_id, instance_id)
);

CREATE TABLE agentic_mesh_v5.audit_records (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('organization', 'project')),
    project_id text,
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    action text NOT NULL CHECK (btrim(action) <> ''),
    object_type text NOT NULL CHECK (btrim(object_type) <> ''),
    object_id text NOT NULL CHECK (btrim(object_id) <> ''),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id) REFERENCES agentic_mesh_v5.projects(project_id)
        ON DELETE CASCADE,
    CHECK (
        (scope = 'organization' AND project_id IS NULL)
        OR (scope = 'project' AND project_id IS NOT NULL)
    )
);
