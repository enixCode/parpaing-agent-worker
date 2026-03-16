-- Agent-Worker schema

CREATE TYPE job_status AS ENUM ('pending', 'running', 'completed', 'failed', 'cancelled');

CREATE TABLE jobs (
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

CREATE INDEX idx_jobs_status_created ON jobs (status, created_at DESC);
CREATE INDEX idx_jobs_created_at ON jobs (created_at DESC);
CREATE INDEX idx_jobs_agent_id ON jobs (agent_id);
CREATE INDEX idx_jobs_cleanup ON jobs (finished_at)
    WHERE status IN ('completed', 'failed', 'cancelled');

-- Container pool (maintained by Tower background task)
CREATE TABLE containers (
    id            SERIAL PRIMARY KEY,
    container_id  TEXT NOT NULL UNIQUE,
    network_id    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ready',  -- ready | busy
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_containers_status ON containers (status, created_at);
