-- Agent-Worker schema (idempotent - safe to re-run at every startup)

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS jobs (
    job_id       text PRIMARY KEY,
    agent_id     text NOT NULL CHECK (agent_id ~ '^[a-zA-Z0-9_-]{1,64}$'),
    status       job_status NOT NULL DEFAULT 'pending',

    -- Request (full AgentRunRequest as JSONB)
    request      jsonb NOT NULL,
    webhook_url  text CHECK (webhook_url IS NULL OR length(webhook_url) <= 2048),

    -- Execution
    container_id text,
    exit_code    int,
    result       jsonb,
    error        text,

    -- Timestamps
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_agent_id ON jobs (agent_id);
CREATE INDEX IF NOT EXISTS idx_jobs_cleanup ON jobs (finished_at)
    WHERE status IN ('completed', 'failed', 'cancelled');

-- Container pool (maintained by Tower background task)
CREATE TABLE IF NOT EXISTS containers (
    id            SERIAL PRIMARY KEY,
    container_id  TEXT NOT NULL UNIQUE,
    network_id    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ready',  -- ready | busy
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_containers_status ON containers (status, created_at);

-- Config store (profiles, engines, templates - managed via API)
DO $$ BEGIN
    CREATE TYPE config_type AS ENUM ('profile', 'engine', 'template');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS configs (
    name        TEXT NOT NULL,
    type        config_type NOT NULL,
    content     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (name, type)
);

CREATE INDEX IF NOT EXISTS idx_configs_type ON configs (type);
