-- ============================================================
-- LA Technologies Task Manager — Database Schema
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- --------------------------------------------------------
-- ORGANIZATION HIERARCHY
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS hierarchy (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    designation TEXT    NOT NULL,
    department  TEXT    NOT NULL,
    email       TEXT,
    manager_id  TEXT,  -- references hierarchy.employee_id
    level       INTEGER NOT NULL DEFAULT 1,  -- 1=CEO, 2=VP, 3=Manager, 4=Lead, 5=Member
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------
-- PROJECTS
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_code    TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL,
    objective       TEXT    NOT NULL,
    type            TEXT    NOT NULL CHECK(type IN ('Operation','Strategy','Marketing','Finance','HR','Funding')),
    priority        TEXT    NOT NULL CHECK(priority IN ('Critical','High','Medium','Low')),
    deadline        TEXT,   -- ISO date YYYY-MM-DD
    status          TEXT    NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Upcoming','Closed')),
    owner_id        TEXT    NOT NULL,  -- references hierarchy.employee_id
    budget          REAL,
    budget_used     REAL    DEFAULT 0,
    tags            TEXT,   -- JSON array of strings
    notes           TEXT,
    created_by      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT
);

-- --------------------------------------------------------
-- PROJECT STAKEHOLDERS
-- (Low-hierarchy member → boss auto-added via API logic)
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_stakeholders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    employee_id    TEXT    NOT NULL,  -- references hierarchy.employee_id
    role           TEXT    NOT NULL DEFAULT 'Contributor',  -- Owner/Lead/Contributor/Reviewer/Informed
    auto_added     INTEGER NOT NULL DEFAULT 0,  -- 1 = added automatically (boss rule)
    added_reason   TEXT,   -- why they were auto-added
    added_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, employee_id)
);

-- --------------------------------------------------------
-- TASKS
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_code           TEXT    NOT NULL UNIQUE,
    project_id          INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title               TEXT    NOT NULL,
    details             TEXT    NOT NULL,
    deadline            TEXT,   -- ISO date YYYY-MM-DD
    status              TEXT    NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Upcoming','Closed')),
    priority            TEXT    NOT NULL CHECK(priority IN ('Critical','High','Medium','Low')),
    last_follow_up_at   TEXT,   -- ISO datetime
    last_follow_up_by   TEXT,   -- employee_id
    last_worked_by      TEXT,   -- employee_id
    last_worked_at      TEXT,   -- ISO datetime
    progress_percentage INTEGER NOT NULL DEFAULT 0 CHECK(progress_percentage BETWEEN 0 AND 100),
    progress_summary    TEXT,
    commitment_date     TEXT,   -- date committed by primary contributor
    created_by          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at           TEXT
);

-- --------------------------------------------------------
-- TASK CONTRIBUTORS
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_contributors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    employee_id     TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'Contributor',  -- Lead/Contributor/Reviewer
    commitment_date TEXT,   -- personal commitment date from this contributor
    is_doing_work   INTEGER NOT NULL DEFAULT 1,  -- 1=actively doing work
    added_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, employee_id)
);

-- --------------------------------------------------------
-- TASK PROGRESS LOG
-- (Full audit trail — every progress update)
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_progress (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    logged_by       TEXT    NOT NULL,  -- employee_id or 'AI_AGENT'
    progress_pct    INTEGER NOT NULL CHECK(progress_pct BETWEEN 0 AND 100),
    summary         TEXT    NOT NULL,
    blockers        TEXT,
    next_steps      TEXT,
    hours_spent     REAL    DEFAULT 0,
    source          TEXT    NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','ai_update','follow_up','status_change')),
    logged_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------
-- FOLLOW-UP LOG
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS follow_ups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    followed_up_by  TEXT    NOT NULL,  -- employee_id
    followed_up_with TEXT   NOT NULL,  -- employee_id of person who was asked
    channel         TEXT    DEFAULT 'Email' CHECK(channel IN ('Email','Call','Meeting','Chat','In-Person')),
    summary         TEXT    NOT NULL,
    response        TEXT,
    next_follow_up  TEXT,  -- suggested next follow-up date
    logged_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------
-- AI INTERACTION LOG
-- (Every AI read/write — full audit for accountability)
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,   -- e.g., 'daily_update', 'project_query', 'task_update'
    payload     TEXT,            -- JSON of what was sent/received
    affected_id TEXT,            -- project_id or task_id
    affected_type TEXT,          -- 'project' or 'task'
    result      TEXT,            -- summary of what AI did
    logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------
-- DAILY DIGEST LOG
-- (AI-generated daily briefings)
-- --------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_digest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date     TEXT NOT NULL UNIQUE,  -- YYYY-MM-DD
    summary         TEXT NOT NULL,
    overdue_tasks   TEXT,   -- JSON array of task_ids
    upcoming_tasks  TEXT,   -- JSON array of task_ids
    critical_flags  TEXT,   -- JSON array of issues
    action_items    TEXT,   -- JSON array of recommended actions
    generated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------
-- INDEXES
-- --------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_projects_status    ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_type      ON projects(type);
CREATE INDEX IF NOT EXISTS idx_projects_deadline  ON projects(deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_project      ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status       ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline     ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_progress_task      ON task_progress(task_id);
CREATE INDEX IF NOT EXISTS idx_followup_task      ON follow_ups(task_id);
CREATE INDEX IF NOT EXISTS idx_ai_log_date        ON ai_log(logged_at);
