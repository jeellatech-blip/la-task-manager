import os
import json
import sqlite3
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional

from flask import Flask, request, jsonify, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'task_manager.db')
SCHEMA_PATH = os.path.join(BASE_DIR, 'schema.sql')

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# ============================================================
# DB HELPERS
# ============================================================
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON;')
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        db.executescript(f.read())
    db.commit()
    db.close()


def rows_to_dict(rows):
    return [dict(r) for r in rows]


def row_to_dict(row):
    return dict(row) if row else None


def now_iso():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def today_iso():
    return date.today().isoformat()


def generate_code(prefix: str, table: str, column: str) -> str:
    db = get_db()
    row = db.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()
    return f"{prefix}-{row['c'] + 1:04d}"

# ============================================================
# HIERARCHY HELPERS
# ============================================================
def get_employee(employee_id: str):
    db = get_db()
    return db.execute("SELECT * FROM hierarchy WHERE employee_id=?", (employee_id,)).fetchone()


def get_manager_chain(employee_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    chain = []
    current = get_employee(employee_id)
    visited = set()
    while current and current['manager_id'] and current['manager_id'] not in visited:
        visited.add(current['employee_id'])
        manager = get_employee(current['manager_id'])
        if not manager:
            break
        chain.append(dict(manager))
        current = manager
    return chain


def auto_add_bosses(project_id: int, employee_ids: List[str]):
    db = get_db()
    for emp_id in employee_ids:
        emp = get_employee(emp_id)
        if not emp:
            continue
        if emp['level'] >= 4:  # lead/member level => add manager chain by default
            chain = get_manager_chain(emp_id)
            for manager in chain:
                try:
                    db.execute(
                        """
                        INSERT INTO project_stakeholders (project_id, employee_id, role, auto_added, added_reason)
                        VALUES (?, ?, 'Informed', 1, ?)
                        """,
                        (project_id, manager['employee_id'], f"Auto-added because {emp['name']} is involved")
                    )
                except sqlite3.IntegrityError:
                    pass
    db.commit()

# ============================================================
# AUDIT LOG
# ============================================================
def log_ai(action: str, payload: Dict[str, Any], affected_type: Optional[str] = None,
           affected_id: Optional[str] = None, result: str = ""):
    db = get_db()
    db.execute(
        """
        INSERT INTO ai_log (action, payload, affected_type, affected_id, result)
        VALUES (?, ?, ?, ?, ?)
        """,
        (action, json.dumps(payload), affected_type, str(affected_id) if affected_id else None, result)
    )
    db.commit()

# ============================================================
# HEALTH
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'task-analyzer-manager',
        'timestamp': now_iso()
    })

# ============================================================
# HIERARCHY ENDPOINTS
# ============================================================
@app.route('/api/hierarchy', methods=['POST'])
def add_employee():
    data = request.get_json(force=True)
    db = get_db()
    db.execute(
        """
        INSERT INTO hierarchy (employee_id, name, designation, department, email, manager_id, level)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data['employee_id'], data['name'], data['designation'], data['department'],
            data.get('email'), data.get('manager_id'), data.get('level', 5)
        )
    )
    db.commit()
    return jsonify({'message': 'employee added', 'employee_id': data['employee_id']}), 201


@app.route('/api/hierarchy', methods=['GET'])
def list_hierarchy():
    db = get_db()
    rows = db.execute("SELECT * FROM hierarchy ORDER BY level ASC, name ASC").fetchall()
    return jsonify(rows_to_dict(rows))

# ============================================================
# PROJECTS
# ============================================================
@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.get_json(force=True)
    db = get_db()
    project_code = generate_code('PRJ', 'projects', 'project_code')

    db.execute(
        """
        INSERT INTO projects (
            project_code, title, objective, type, priority, deadline, status,
            owner_id, budget, budget_used, tags, notes, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_code,
            data['title'],
            data['objective'],
            data['type'],
            data['priority'],
            data.get('deadline'),
            data.get('status', 'Active'),
            data['owner_id'],
            data.get('budget'),
            data.get('budget_used', 0),
            json.dumps(data.get('tags', [])),
            data.get('notes'),
            data.get('created_by', 'SYSTEM')
        )
    )
    project_id = db.execute('SELECT last_insert_rowid() as id').fetchone()['id']

    stakeholder_ids = list(set(data.get('stakeholder_ids', []) + [data['owner_id']]))
    for emp_id in stakeholder_ids:
        try:
            db.execute(
                "INSERT INTO project_stakeholders (project_id, employee_id, role, auto_added) VALUES (?, ?, ?, 0)",
                (project_id, emp_id, 'Lead' if emp_id == data['owner_id'] else 'Contributor')
            )
        except sqlite3.IntegrityError:
            pass

    db.commit()
    auto_add_bosses(project_id, stakeholder_ids)

    return jsonify({'message': 'project created', 'project_id': project_id, 'project_code': project_code}), 201


@app.route('/api/projects', methods=['GET'])
def list_projects():
    db = get_db()
    filters = []
    values = []

    if request.args.get('status'):
        filters.append('p.status = ?')
        values.append(request.args['status'])
    if request.args.get('type'):
        filters.append('p.type = ?')
        values.append(request.args['type'])
    if request.args.get('priority'):
        filters.append('p.priority = ?')
        values.append(request.args['priority'])

    where_clause = ('WHERE ' + ' AND '.join(filters)) if filters else ''
    query = f"""
        SELECT p.*, h.name as owner_name, h.designation as owner_designation
        FROM projects p
        LEFT JOIN hierarchy h ON p.owner_id = h.employee_id
        {where_clause}
        ORDER BY
            CASE p.priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END,
            p.deadline ASC
    """
    rows = db.execute(query, values).fetchall()
    return jsonify(rows_to_dict(rows))


@app.route('/api/projects/<int:project_id>', methods=['GET'])
def get_project(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        return jsonify({'error': 'project not found'}), 404

    stakeholders = db.execute(
        """
        SELECT ps.*, h.name, h.designation, h.department
        FROM project_stakeholders ps
        JOIN hierarchy h ON ps.employee_id = h.employee_id
        WHERE ps.project_id=?
        ORDER BY ps.auto_added ASC, h.level ASC, h.name ASC
        """,
        (project_id,)
    ).fetchall()

    tasks = db.execute(
        "SELECT * FROM tasks WHERE project_id=? ORDER BY deadline ASC, updated_at DESC",
        (project_id,)
    ).fetchall()

    return jsonify({
        'project': row_to_dict(project),
        'stakeholders': rows_to_dict(stakeholders),
        'tasks': rows_to_dict(tasks)
    })


@app.route('/api/projects/<int:project_id>', methods=['PUT'])
def update_project(project_id):
    data = request.get_json(force=True)
    db = get_db()
    current = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not current:
        return jsonify({'error': 'project not found'}), 404

    db.execute(
        """
        UPDATE projects SET
            title=?, objective=?, type=?, priority=?, deadline=?, status=?,
            budget=?, budget_used=?, tags=?, notes=?, updated_at=?,
            closed_at=CASE WHEN ?='Closed' AND closed_at IS NULL THEN datetime('now') ELSE closed_at END
        WHERE id=?
        """,
        (
            data.get('title', current['title']),
            data.get('objective', current['objective']),
            data.get('type', current['type']),
            data.get('priority', current['priority']),
            data.get('deadline', current['deadline']),
            data.get('status', current['status']),
            data.get('budget', current['budget']),
            data.get('budget_used', current['budget_used']),
            json.dumps(data.get('tags', json.loads(current['tags']) if current['tags'] else [])),
            data.get('notes', current['notes']),
            now_iso(),
            data.get('status', current['status']),
            project_id
        )
    )
    db.commit()
    return jsonify({'message': 'project updated', 'project_id': project_id})

# ============================================================
# TASKS
# ============================================================
@app.route('/api/tasks', methods=['POST'])
def create_task():
    data = request.get_json(force=True)
    db = get_db()
    task_code = generate_code('TSK', 'tasks', 'task_code')

    db.execute(
        """
        INSERT INTO tasks (
            task_code, project_id, title, details, deadline, status, priority,
            progress_summary, commitment_date, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_code,
            data['project_id'],
            data['title'],
            data['details'],
            data.get('deadline'),
            data.get('status', 'Active'),
            data.get('priority', 'Medium'),
            data.get('progress_summary', 'Task created.'),
            data.get('commitment_date'),
            data.get('created_by', 'SYSTEM')
        )
    )
    task_id = db.execute('SELECT last_insert_rowid() as id').fetchone()['id']

    contributors = data.get('contributors', [])
    for c in contributors:
        db.execute(
            """
            INSERT INTO task_contributors (task_id, employee_id, role, commitment_date, is_doing_work)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, c['employee_id'], c.get('role', 'Contributor'), c.get('commitment_date'), c.get('is_doing_work', 1))
        )

    db.execute(
        """
        INSERT INTO task_progress (task_id, logged_by, progress_pct, summary, blockers, next_steps, source)
        VALUES (?, ?, ?, ?, ?, ?, 'manual')
        """,
        (
            task_id,
            data.get('created_by', 'SYSTEM'),
            0,
            data.get('progress_summary', 'Task created.'),
            data.get('blockers'),
            data.get('next_steps')
        )
    )

    db.commit()
    return jsonify({'message': 'task created', 'task_id': task_id, 'task_code': task_code}), 201


@app.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': 'task not found'}), 404

    contributors = db.execute(
        """
        SELECT tc.*, h.name, h.designation, h.department
        FROM task_contributors tc
        JOIN hierarchy h ON tc.employee_id = h.employee_id
        WHERE tc.task_id=?
        ORDER BY h.level ASC, h.name ASC
        """,
        (task_id,)
    ).fetchall()

    progress = db.execute(
        "SELECT * FROM task_progress WHERE task_id=? ORDER BY logged_at DESC, id DESC",
        (task_id,)
    ).fetchall()

    followups = db.execute(
        "SELECT * FROM follow_ups WHERE task_id=? ORDER BY logged_at DESC, id DESC",
        (task_id,)
    ).fetchall()

    return jsonify({
        'task': row_to_dict(task),
        'contributors': rows_to_dict(contributors),
        'progress_log': rows_to_dict(progress),
        'follow_ups': rows_to_dict(followups)
    })


@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
def update_task(task_id):
    data = request.get_json(force=True)
    db = get_db()
    current = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not current:
        return jsonify({'error': 'task not found'}), 404

    db.execute(
        """
        UPDATE tasks SET
            title=?, details=?, deadline=?, status=?, priority=?,
            last_follow_up_at=?, last_follow_up_by=?, last_worked_by=?, last_worked_at=?,
            progress_percentage=?, progress_summary=?, commitment_date=?, updated_at=?,
            closed_at=CASE WHEN ?='Closed' AND closed_at IS NULL THEN datetime('now') ELSE closed_at END
        WHERE id=?
        """,
        (
            data.get('title', current['title']),
            data.get('details', current['details']),
            data.get('deadline', current['deadline']),
            data.get('status', current['status']),
            data.get('priority', current['priority']),
            data.get('last_follow_up_at', current['last_follow_up_at']),
            data.get('last_follow_up_by', current['last_follow_up_by']),
            data.get('last_worked_by', current['last_worked_by']),
            data.get('last_worked_at', current['last_worked_at']),
            data.get('progress_percentage', current['progress_percentage']),
            data.get('progress_summary', current['progress_summary']),
            data.get('commitment_date', current['commitment_date']),
            now_iso(),
            data.get('status', current['status']),
            task_id
        )
    )

    if 'progress_percentage' in data or 'progress_summary' in data:
        db.execute(
            """
            INSERT INTO task_progress (task_id, logged_by, progress_pct, summary, blockers, next_steps, hours_spent, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                data.get('logged_by', 'SYSTEM'),
                data.get('progress_percentage', current['progress_percentage']),
                data.get('progress_summary', current['progress_summary'] or 'Task updated'),
                data.get('blockers'),
                data.get('next_steps'),
                data.get('hours_spent', 0),
                data.get('source', 'status_change')
            )
        )

    db.commit()
    return jsonify({'message': 'task updated', 'task_id': task_id})

# ============================================================
# PROGRESS + FOLLOW-UP
# ============================================================
@app.route('/api/tasks/<int:task_id>/progress', methods=['POST'])
def add_progress(task_id):
    data = request.get_json(force=True)
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': 'task not found'}), 404

    db.execute(
        """
        INSERT INTO task_progress (task_id, logged_by, progress_pct, summary, blockers, next_steps, hours_spent, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            data['logged_by'],
            data['progress_pct'],
            data['summary'],
            data.get('blockers'),
            data.get('next_steps'),
            data.get('hours_spent', 0),
            data.get('source', 'manual')
        )
    )

    db.execute(
        """
        UPDATE tasks SET
            progress_percentage=?, progress_summary=?, last_worked_by=?, last_worked_at=?, updated_at=?,
            status=CASE WHEN ? >= 100 THEN 'Closed' ELSE status END,
            closed_at=CASE WHEN ? >= 100 AND closed_at IS NULL THEN datetime('now') ELSE closed_at END
        WHERE id=?
        """,
        (
            data['progress_pct'],
            data['summary'],
            data.get('logged_by'),
            now_iso(),
            now_iso(),
            data['progress_pct'],
            data['progress_pct'],
            task_id
        )
    )

    db.commit()
    return jsonify({'message': 'progress logged', 'task_id': task_id})


@app.route('/api/tasks/<int:task_id>/follow-up', methods=['POST'])
def add_follow_up(task_id):
    data = request.get_json(force=True)
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return jsonify({'error': 'task not found'}), 404

    db.execute(
        """
        INSERT INTO follow_ups (task_id, followed_up_by, followed_up_with, channel, summary, response, next_follow_up)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            data['followed_up_by'],
            data['followed_up_with'],
            data.get('channel', 'Email'),
            data['summary'],
            data.get('response'),
            data.get('next_follow_up')
        )
    )

    db.execute(
        """
        UPDATE tasks SET last_follow_up_at=?, last_follow_up_by=?, updated_at=? WHERE id=?
        """,
        (now_iso(), data['followed_up_by'], now_iso(), task_id)
    )

    db.commit()
    return jsonify({'message': 'follow-up logged', 'task_id': task_id})

# ============================================================
# DASHBOARD / ANALYZER
# ============================================================
@app.route('/api/analyzer/dashboard', methods=['GET'])
def dashboard():
    db = get_db()
    today = today_iso()
    next_7 = (date.today() + timedelta(days=7)).isoformat()

    summary = {
        'total_projects': db.execute("SELECT COUNT(*) c FROM projects").fetchone()['c'],
        'active_projects': db.execute("SELECT COUNT(*) c FROM projects WHERE status='Active'").fetchone()['c'],
        'upcoming_projects': db.execute("SELECT COUNT(*) c FROM projects WHERE status='Upcoming'").fetchone()['c'],
        'closed_projects': db.execute("SELECT COUNT(*) c FROM projects WHERE status='Closed'").fetchone()['c'],
        'total_tasks': db.execute("SELECT COUNT(*) c FROM tasks").fetchone()['c'],
        'active_tasks': db.execute("SELECT COUNT(*) c FROM tasks WHERE status='Active'").fetchone()['c'],
        'upcoming_tasks': db.execute("SELECT COUNT(*) c FROM tasks WHERE status='Upcoming'").fetchone()['c'],
        'closed_tasks': db.execute("SELECT COUNT(*) c FROM tasks WHERE status='Closed'").fetchone()['c'],
        'overdue_tasks': db.execute("SELECT COUNT(*) c FROM tasks WHERE status!='Closed' AND deadline < ?", (today,)).fetchone()['c'],
        'due_next_7_days': db.execute("SELECT COUNT(*) c FROM tasks WHERE status!='Closed' AND deadline BETWEEN ? AND ?", (today, next_7)).fetchone()['c'],
        'critical_tasks': db.execute("SELECT COUNT(*) c FROM tasks WHERE status!='Closed' AND priority='Critical'").fetchone()['c']
    }

    overdue = db.execute(
        """
        SELECT t.*, p.title as project_title
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status!='Closed' AND t.deadline < ?
        ORDER BY t.deadline ASC
        LIMIT 25
        """,
        (today,)
    ).fetchall()

    stale = db.execute(
        """
        SELECT t.*, p.title as project_title
        FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status='Active' AND (
            t.last_follow_up_at IS NULL OR datetime(t.last_follow_up_at) < datetime('now', '-3 days')
        )
        ORDER BY t.updated_at ASC
        LIMIT 25
        """
    ).fetchall()

    return jsonify({
        'summary': summary,
        'overdue_tasks': rows_to_dict(overdue),
        'stale_followups': rows_to_dict(stale)
    })

# ============================================================
# AI OPEN NODES / AGENT API
# ============================================================
@app.route('/api/ai/context', methods=['GET'])
def ai_context():
    """
    Full context snapshot for AI.
    This is the main open node the AI can read daily.
    """
    db = get_db()
    projects = rows_to_dict(db.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall())
    tasks = rows_to_dict(db.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall())
    progress = rows_to_dict(db.execute("SELECT * FROM task_progress ORDER BY logged_at DESC LIMIT 200").fetchall())
    hierarchy = rows_to_dict(db.execute("SELECT * FROM hierarchy WHERE is_active=1 ORDER BY level ASC").fetchall())

    payload = {
        'generated_at': now_iso(),
        'projects': projects,
        'tasks': tasks,
        'recent_progress': progress,
        'hierarchy': hierarchy
    }
    log_ai('context_read', {'endpoint': '/api/ai/context'}, result='AI fetched full context')
    return jsonify(payload)


@app.route('/api/ai/daily-brief', methods=['POST'])
def ai_daily_brief():
    """
    AI can call this once daily or you can schedule it.
    Returns overdue items, due-soon items, stalled items, and recommendations.
    """
    db = get_db()
    today = date.today()
    next_3 = (today + timedelta(days=3)).isoformat()
    today_str = today.isoformat()

    overdue_rows = rows_to_dict(db.execute(
        "SELECT id, task_code, title, deadline, priority, progress_percentage FROM tasks WHERE status!='Closed' AND deadline < ? ORDER BY deadline ASC",
        (today_str,)
    ).fetchall())

    upcoming_rows = rows_to_dict(db.execute(
        "SELECT id, task_code, title, deadline, priority, progress_percentage FROM tasks WHERE status!='Closed' AND deadline BETWEEN ? AND ? ORDER BY deadline ASC",
        (today_str, next_3)
    ).fetchall())

    stalled_rows = rows_to_dict(db.execute(
        "SELECT id, task_code, title, updated_at, last_follow_up_at FROM tasks WHERE status='Active' AND (last_follow_up_at IS NULL OR datetime(last_follow_up_at) < datetime('now', '-3 days')) ORDER BY updated_at ASC"
    ).fetchall())

    critical_flags = []
    action_items = []

    for t in overdue_rows:
        critical_flags.append(f"Task {t['task_code']} is overdue since {t['deadline']}")
        action_items.append(f"Immediate owner escalation for {t['task_code']}")
    for t in upcoming_rows:
        if t['priority'] in ('Critical', 'High'):
            action_items.append(f"Check readiness for {t['task_code']} due on {t['deadline']}")
    for t in stalled_rows[:10]:
        action_items.append(f"Take follow-up on stalled task {t['task_code']}")

    summary = {
        'date': today_str,
        'overdue_count': len(overdue_rows),
        'due_soon_count': len(upcoming_rows),
        'stalled_count': len(stalled_rows),
        'top_message': f"{len(overdue_rows)} overdue, {len(upcoming_rows)} due in 3 days, {len(stalled_rows)} stale follow-up tasks"
    }

    db.execute(
        """
        INSERT INTO daily_digest (digest_date, summary, overdue_tasks, upcoming_tasks, critical_flags, action_items)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(digest_date) DO UPDATE SET
            summary=excluded.summary,
            overdue_tasks=excluded.overdue_tasks,
            upcoming_tasks=excluded.upcoming_tasks,
            critical_flags=excluded.critical_flags,
            action_items=excluded.action_items,
            generated_at=datetime('now')
        """,
        (
            today_str,
            json.dumps(summary),
            json.dumps([t['id'] for t in overdue_rows]),
            json.dumps([t['id'] for t in upcoming_rows]),
            json.dumps(critical_flags),
            json.dumps(action_items)
        )
    )
    db.commit()

    payload = {
        'summary': summary,
        'overdue_tasks': overdue_rows,
        'upcoming_tasks': upcoming_rows,
        'stalled_tasks': stalled_rows,
        'critical_flags': critical_flags,
        'action_items': action_items
    }
    log_ai('daily_update', payload, result='AI daily brief generated')
    return jsonify(payload)


@app.route('/api/ai/update-task', methods=['POST'])
def ai_update_task():
    """
    Open node for AI to update a task based on daily conversation or agent reasoning.
    Expected JSON:
    {
      "task_id": 1,
      "progress_pct": 55,
      "summary": "Draft completed, internal review pending",
      "blockers": "Waiting for finance input",
      "next_steps": "Get finance approval and send final version",
      "logged_by": "AI_AGENT"
    }
    """
    data = request.get_json(force=True)
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (data['task_id'],)).fetchone()
    if not task:
        return jsonify({'error': 'task not found'}), 404

    db.execute(
        """
        INSERT INTO task_progress (task_id, logged_by, progress_pct, summary, blockers, next_steps, source)
        VALUES (?, ?, ?, ?, ?, ?, 'ai_update')
        """,
        (
            data['task_id'],
            data.get('logged_by', 'AI_AGENT'),
            data['progress_pct'],
            data['summary'],
            data.get('blockers'),
            data.get('next_steps')
        )
    )

    db.execute(
        """
        UPDATE tasks SET
            progress_percentage=?, progress_summary=?, last_worked_by=?, last_worked_at=?, updated_at=?,
            status=CASE WHEN ? >= 100 THEN 'Closed' ELSE status END,
            closed_at=CASE WHEN ? >= 100 AND closed_at IS NULL THEN datetime('now') ELSE closed_at END
        WHERE id=?
        """,
        (
            data['progress_pct'],
            data['summary'],
            data.get('logged_by', 'AI_AGENT'),
            now_iso(),
            now_iso(),
            data['progress_pct'],
            data['progress_pct'],
            data['task_id']
        )
    )
    db.commit()

    log_ai('task_update', data, affected_type='task', affected_id=data['task_id'], result='AI updated task progress')
    return jsonify({'message': 'AI task update applied', 'task_id': data['task_id']})


@app.route('/api/ai/create-follow-up', methods=['POST'])
def ai_create_followup():
    data = request.get_json(force=True)
    db = get_db()
    db.execute(
        """
        INSERT INTO follow_ups (task_id, followed_up_by, followed_up_with, channel, summary, response, next_follow_up)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data['task_id'],
            data.get('followed_up_by', 'AI_AGENT'),
            data['followed_up_with'],
            data.get('channel', 'Chat'),
            data['summary'],
            data.get('response'),
            data.get('next_follow_up')
        )
    )
    db.execute(
        "UPDATE tasks SET last_follow_up_at=?, last_follow_up_by=?, updated_at=? WHERE id=?",
        (now_iso(), data.get('followed_up_by', 'AI_AGENT'), now_iso(), data['task_id'])
    )
    db.commit()
    log_ai('follow_up_created', data, affected_type='task', affected_id=data['task_id'], result='AI logged follow-up')
    return jsonify({'message': 'AI follow-up created', 'task_id': data['task_id']})


@app.route('/api/ai/recommendations', methods=['GET'])
def ai_recommendations():
    db = get_db()
    today = today_iso()

    overdue = rows_to_dict(db.execute(
        "SELECT task_code, title, deadline, priority FROM tasks WHERE status!='Closed' AND deadline < ? ORDER BY deadline ASC",
        (today,)
    ).fetchall())
    stale = rows_to_dict(db.execute(
        "SELECT task_code, title, last_follow_up_at, priority FROM tasks WHERE status='Active' AND (last_follow_up_at IS NULL OR datetime(last_follow_up_at) < datetime('now', '-3 days')) ORDER BY priority DESC, updated_at ASC"
    ).fetchall())
    high_risk_projects = rows_to_dict(db.execute(
        """
        SELECT p.id, p.project_code, p.title,
               SUM(CASE WHEN t.status!='Closed' AND t.deadline < date('now') THEN 1 ELSE 0 END) as overdue_count,
               SUM(CASE WHEN t.priority IN ('Critical','High') AND t.status!='Closed' THEN 1 ELSE 0 END) as high_priority_open
        FROM projects p
        LEFT JOIN tasks t ON p.id = t.project_id
        WHERE p.status!='Closed'
        GROUP BY p.id, p.project_code, p.title
        HAVING overdue_count > 0 OR high_priority_open > 2
        ORDER BY overdue_count DESC, high_priority_open DESC
        """
    ).fetchall())

    recommendations = []
    for t in overdue[:10]:
        recommendations.append({
            'type': 'task_escalation',
            'message': f"Escalate {t['task_code']} ({t['title']}) — overdue since {t['deadline']}",
            'priority': t['priority']
        })
    for t in stale[:10]:
        recommendations.append({
            'type': 'follow_up_needed',
            'message': f"Take follow-up on {t['task_code']} ({t['title']})",
            'priority': t['priority']
        })
    for p in high_risk_projects[:10]:
        recommendations.append({
            'type': 'project_risk',
            'message': f"Review {p['project_code']} — {p['overdue_count']} overdue task(s), {p['high_priority_open']} high-priority open task(s)",
            'priority': 'High'
        })

    payload = {'generated_at': now_iso(), 'recommendations': recommendations}
    log_ai('recommendations_generated', payload, result='AI recommendations generated')
    return jsonify(payload)

# ============================================================
# SIMPLE BOOTSTRAP / SEED
# ============================================================
@app.route('/api/seed', methods=['POST'])
def seed_demo_data():
    db = get_db()
    existing = db.execute("SELECT COUNT(*) c FROM hierarchy").fetchone()['c']
    if existing > 0:
        return jsonify({'message': 'seed skipped; data already exists'})

    employees = [
        ('EMP001', 'Aarav Mehta', 'CEO', 'Leadership', 'aarav@company.com', None, 1),
        ('EMP002', 'Neha Iyer', 'COO', 'Operations', 'neha@company.com', 'EMP001', 2),
        ('EMP003', 'Rahul Shah', 'Operations Manager', 'Operations', 'rahul@company.com', 'EMP002', 3),
        ('EMP004', 'Priya Nair', 'Senior Analyst', 'Operations', 'priya@company.com', 'EMP003', 4),
        ('EMP005', 'Kabir Jain', 'Associate', 'Operations', 'kabir@company.com', 'EMP004', 5),
        ('EMP006', 'Ritika Das', 'Finance Manager', 'Finance', 'ritika@company.com', 'EMP002', 3),
    ]
    db.executemany(
        "INSERT INTO hierarchy (employee_id, name, designation, department, email, manager_id, level) VALUES (?, ?, ?, ?, ?, ?, ?)",
        employees
    )

    db.execute(
        """
        INSERT INTO projects (project_code, title, objective, type, priority, deadline, status, owner_id, created_by)
        VALUES ('PRJ-0001', 'Q2 Operating Rhythm', 'Improve weekly execution governance and follow-up visibility', 'Operation', 'High', date('now', '+20 day'), 'Active', 'EMP003', 'SYSTEM')
        """
    )
    project_id = db.execute("SELECT id FROM projects WHERE project_code='PRJ-0001'").fetchone()['id']

    stakeholders = [
        (project_id, 'EMP003', 'Lead', 0, None),
        (project_id, 'EMP004', 'Contributor', 0, None),
        (project_id, 'EMP005', 'Contributor', 0, None),
        (project_id, 'EMP002', 'Informed', 1, 'Auto-added because lower hierarchy team members are involved'),
        (project_id, 'EMP001', 'Informed', 1, 'Auto-added because lower hierarchy team members are involved')
    ]
    db.executemany(
        "INSERT INTO project_stakeholders (project_id, employee_id, role, auto_added, added_reason) VALUES (?, ?, ?, ?, ?)",
        stakeholders
    )

    db.execute(
        """
        INSERT INTO tasks (task_code, project_id, title, details, deadline, status, priority, progress_percentage, progress_summary, created_by)
        VALUES ('TSK-0001', ?, 'Create weekly review tracker', 'Set up a structured tracker for team-level weekly reporting and deadline review', date('now', '+5 day'), 'Active', 'High', 35, 'Tracker template drafted; pending finance and HR columns', 'SYSTEM')
        """,
        (project_id,)
    )
    task_id = db.execute("SELECT id FROM tasks WHERE task_code='TSK-0001'").fetchone()['id']

    task_contributors = [
        (task_id, 'EMP004', 'Lead', date.today().isoformat(), 1),
        (task_id, 'EMP005', 'Contributor', (date.today() + timedelta(days=2)).isoformat(), 1),
        (task_id, 'EMP006', 'Reviewer', (date.today() + timedelta(days=3)).isoformat(), 0),
    ]
    db.executemany(
        "INSERT INTO task_contributors (task_id, employee_id, role, commitment_date, is_doing_work) VALUES (?, ?, ?, ?, ?)",
        task_contributors
    )

    db.execute(
        """
        INSERT INTO task_progress (task_id, logged_by, progress_pct, summary, blockers, next_steps, source)
        VALUES (?, 'EMP004', 35, 'Tracker template drafted; pending finance and HR columns', 'Need finance structure confirmation', 'Get finance inputs and finalize template', 'manual')
        """,
        (task_id,)
    )

    db.commit()
    return jsonify({'message': 'demo data seeded'})


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
