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

CREATE FUNCTION agentic_mesh_v5.ensure_maintenance_write_guards()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT class.relname AS table_name, class.oid AS table_oid
        FROM pg_class AS class
        JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
        WHERE namespace.nspname = 'agentic_mesh_v5'
          AND class.relkind IN ('r', 'p')
          AND class.relname <> 'database_maintenance'
        ORDER BY class.relname
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = target.table_oid
              AND tgname = 'maintenance_write_guard'
              AND NOT tgisinternal
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER maintenance_write_guard '
                'BEFORE INSERT OR UPDATE OR DELETE ON agentic_mesh_v5.%I '
                'FOR EACH ROW EXECUTE FUNCTION '
                'agentic_mesh_v5.reject_writes_during_maintenance()',
                target.table_name
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = target.table_oid
              AND tgname = 'maintenance_truncate_guard'
              AND NOT tgisinternal
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER maintenance_truncate_guard '
                'BEFORE TRUNCATE ON agentic_mesh_v5.%I '
                'FOR EACH STATEMENT EXECUTE FUNCTION '
                'agentic_mesh_v5.reject_writes_during_maintenance()',
                target.table_name
            );
        END IF;
    END LOOP;
END;
$$;

SELECT agentic_mesh_v5.ensure_maintenance_write_guards();
