ALTER TABLE agentic_mesh_v5.shared_memory_entries
    ADD COLUMN classification text NOT NULL DEFAULT 'project_specific'
        CHECK (classification IN ('project_specific', 'organization_generic', 'uncertain')),
    ADD COLUMN policy_evidence jsonb NOT NULL DEFAULT
        '{"policy_version":"migration-default","reasons":["pre-classification-entry"]}'::jsonb
        CHECK (jsonb_typeof(policy_evidence) = 'object'),
    ADD COLUMN redactions jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(redactions) = 'array');

ALTER TABLE agentic_mesh_v5.shared_memory_revisions
    ADD COLUMN classification text NOT NULL DEFAULT 'project_specific'
        CHECK (classification IN ('project_specific', 'organization_generic', 'uncertain')),
    ADD COLUMN policy_evidence jsonb NOT NULL DEFAULT
        '{"policy_version":"migration-default","reasons":["pre-classification-entry"]}'::jsonb
        CHECK (jsonb_typeof(policy_evidence) = 'object'),
    ADD COLUMN redactions jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(redactions) = 'array');

UPDATE agentic_mesh_v5.shared_memory_entries
SET classification = 'organization_generic',
    policy_evidence =
        '{"policy_version":"migration-default","reasons":["legacy-organization-entry"]}'::jsonb
WHERE scope = 'organization_role';

UPDATE agentic_mesh_v5.shared_memory_revisions
SET classification = 'organization_generic',
    policy_evidence =
        '{"policy_version":"migration-default","reasons":["legacy-organization-entry"]}'::jsonb
WHERE scope = 'organization_role';

ALTER TABLE agentic_mesh_v5.shared_memory_entries
    ADD CHECK (scope <> 'organization_role' OR classification = 'organization_generic');

ALTER TABLE agentic_mesh_v5.shared_memory_revisions
    ADD CHECK (scope <> 'organization_role' OR classification = 'organization_generic');
