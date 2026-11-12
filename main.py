from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field

DB_PATH = Path(__file__).resolve().parent / "data" / "app.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Task Management API",
    version="1.0.0",
    description="A small Users and Tasks API with SQLite storage.",
)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo'
                CHECK(status IN ('todo', 'in_progress', 'done')),
            due_date TEXT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        """)


@app.on_event("startup")
def startup():
    init_db()


class UserInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    user_id: int
    description: str = ""
    status: str = "todo"
    due_date: date | None = None


def now():
    return datetime.now(timezone.utc).isoformat()


def row_dict(row):
    return dict(row) if row is not None else None


def get_or_404(db, table, item_id, label):
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": f"{label} with id {item_id} not found"})
    return row


def validate_status(value):
    if value not in {"todo", "in_progress", "done"}:
        raise HTTPException(status_code=400, detail={"error": "INVALID_STATUS", "message": "status must be todo, in_progress, or done"})


@app.exception_handler(HTTPException)
async def api_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        payload = detail
    else:
        payload = {"error": "HTTP_ERROR", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)


@app.get("/")
def root():
    return {"message": "Task Management API", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok"}


# Users
@app.get("/api/users")
def list_users():
    with connect() as db:
        return [row_dict(r) for r in db.execute("SELECT * FROM users ORDER BY id").fetchall()]


@app.post("/api/users", status_code=status.HTTP_201_CREATED)
def create_user(data: UserInput):
    with connect() as db:
        try:
            cur = db.execute(
                "INSERT INTO users(name, email, created_at) VALUES (?, ?, ?)",
                (data.name, str(data.email), now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail={"error": "EMAIL_EXISTS", "message": "A user with this email already exists"})
        return row_dict(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.get("/api/users/{user_id}")
def get_user(user_id: int):
    with connect() as db:
        return row_dict(get_or_404(db, "users", user_id, "User"))


@app.put("/api/users/{user_id}")
def replace_user(user_id: int, data: UserInput):
    with connect() as db:
        get_or_404(db, "users", user_id, "User")
        try:
            db.execute("UPDATE users SET name = ?, email = ? WHERE id = ?", (data.name, str(data.email), user_id))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail={"error": "EMAIL_EXISTS", "message": "A user with this email already exists"})
        return row_dict(get_or_404(db, "users", user_id, "User"))


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, data: dict):
    with connect() as db:
        current = row_dict(get_or_404(db, "users", user_id, "User"))
        name = data.get("name", current["name"])
        email = data.get("email", current["email"])
        try:
            db.execute("UPDATE users SET name = ?, email = ? WHERE id = ?", (name, email, user_id))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail={"error": "EMAIL_EXISTS", "message": "A user with this email already exists"})
        return row_dict(get_or_404(db, "users", user_id, "User"))


@app.delete("/api/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int):
    with connect() as db:
        get_or_404(db, "users", user_id, "User")
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return Response(status_code=204)


@app.get("/api/users/{user_id}/tasks")
def list_user_tasks(user_id: int):
    with connect() as db:
        get_or_404(db, "users", user_id, "User")
        rows = db.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        return [row_dict(r) for r in rows]


# Tasks
@app.get("/api/tasks")
def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: int | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
):
    with connect() as db:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status_filter:
            validate_status(status_filter)
            sql += " AND status = ?"
            params.append(status_filter)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id LIMIT ? OFFSET ?"
        params.extend([limit, (page - 1) * limit])
        return [row_dict(r) for r in db.execute(sql, params).fetchall()]


@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
def create_task(data: TaskInput):
    validate_status(data.status)
    with connect() as db:
        get_or_404(db, "users", data.user_id, "User")
        cur = db.execute(
            "INSERT INTO tasks(title, description, status, due_date, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (data.title, data.description, data.status, data.due_date.isoformat() if data.due_date else None, data.user_id, now()),
        )
        return row_dict(db.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    with connect() as db:
        return row_dict(get_or_404(db, "tasks", task_id, "Task"))


@app.put("/api/tasks/{task_id}")
def replace_task(task_id: int, data: TaskInput):
    validate_status(data.status)
    with connect() as db:
        get_or_404(db, "tasks", task_id, "Task")
        get_or_404(db, "users", data.user_id, "User")
        db.execute(
            "UPDATE tasks SET title=?, description=?, status=?, due_date=?, user_id=? WHERE id=?",
            (data.title, data.description, data.status, data.due_date.isoformat() if data.due_date else None, data.user_id, task_id),
        )
        return row_dict(get_or_404(db, "tasks", task_id, "Task"))


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, data: dict):
    with connect() as db:
        current = row_dict(get_or_404(db, "tasks", task_id, "Task"))
        values = {key: data.get(key, current[key]) for key in ("title", "description", "status", "due_date", "user_id")}
        validate_status(values["status"])
        get_or_404(db, "users", int(values["user_id"]), "User")
        due = values["due_date"]
        if isinstance(due, date):
            due = due.isoformat()
        db.execute(
            "UPDATE tasks SET title=?, description=?, status=?, due_date=?, user_id=? WHERE id=?",
            (values["title"], values["description"], values["status"], due, values["user_id"], task_id),
        )
        return row_dict(get_or_404(db, "tasks", task_id, "Task"))


@app.delete("/api/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int):
    with connect() as db:
        get_or_404(db, "tasks", task_id, "Task")
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return Response(status_code=204)
