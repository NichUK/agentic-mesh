CREATE TABLE agentic_mesh_v5.database_maintenance (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    status text NOT NULL CHECK (status IN ('active', 'paused')),
    reason text NOT NULL DEFAULT '',
    changed_by text NOT NULL CHECK (btrim(changed_by) <> ''),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO agentic_mesh_v5.database_maintenance
    (singleton, status, reason, changed_by)
VALUES (true, 'active', 'initial state', 'migration-0006');

CREATE TABLE agentic_mesh_v5.database_maintenance_history (
    transition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('active', 'paused')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    changed_by text NOT NULL CHECK (btrim(changed_by) <> ''),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO agentic_mesh_v5.database_maintenance_history
    (status, reason, changed_by)
VALUES ('active', 'initial state', 'migration-0006');

CREATE FUNCTION agentic_mesh_v5.reject_writes_during_maintenance()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM agentic_mesh_v5.database_maintenance
        WHERE singleton AND status = 'paused'
    ) THEN
        RAISE EXCEPTION 'database maintenance pause is active'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'agentic_mesh_v5'
          AND tablename <> 'database_maintenance'
        ORDER BY tablename
    LOOP
        EXECUTE format(
            'CREATE TRIGGER maintenance_write_guard '
            'BEFORE INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.%I '
            'FOR EACH ROW EXECUTE FUNCTION '
            'agentic_mesh_v5.reject_writes_during_maintenance()',
            target.tablename
        );
    END LOOP;
END;
$$;
