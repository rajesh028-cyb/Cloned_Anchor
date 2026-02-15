# ANCHOR Passive Observer Store (Blueprint)
# ==========================================
# Flask Blueprint that stores per-session, per-turn forensic snapshots.
# Mounted into the main Anchor app at /observer/*.
#
# Also exposes store_event() for direct in-process writes so Anchor
# never needs an HTTP round-trip to itself.
#
# Storage: SQLite (append-only, WAL mode, no deletes, no updates).

import json
import os
import sqlite3
import time

from flask import Blueprint, request, jsonify

# ── Blueprint ────────────────────────────────────────────────────────────
observer_bp = Blueprint("observer", __name__)

# ── Database ─────────────────────────────────────────────────────────────
DB_PATH = os.getenv("OBSERVER_DB_PATH", "observer_store.db")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_observer_db():
    """Create the events table if it doesn't exist. Append-only by design."""
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL,
            timestamp   REAL    NOT NULL,
            turn        INTEGER NOT NULL,
            state       TEXT    NOT NULL,
            behavior    TEXT    NOT NULL,
            artifacts   TEXT    NOT NULL,
            osint       TEXT    NOT NULL,
            response    TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_session
        ON events (session_id, turn)
    """)
    conn.commit()
    conn.close()


# ── Direct in-process write (no HTTP needed) ─────────────────────────────
def store_event(payload: dict) -> None:
    """
    Append an observer event directly to SQLite.
    Called from anchor_api_server in a daemon thread.
    Caller is responsible for exception handling.
    """
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO events (session_id, timestamp, turn, state, behavior, artifacts, osint, response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("session_id", ""),
            payload.get("timestamp", time.time()),
            payload.get("turn", 0),
            payload.get("state", "CLARIFY"),
            json.dumps(payload.get("behavior", {})),
            json.dumps(payload.get("artifacts", {})),
            json.dumps(payload.get("osint", {})),
            payload.get("response", ""),
        ),
    )
    conn.commit()
    conn.close()


# ── Endpoints (public-read, no API key) ──────────────────────────────────

@observer_bp.route("/observe", methods=["POST"])
def observe():
    """Append a single turn event via HTTP."""
    try:
        data = request.get_json(silent=True)
        if not data or "session_id" not in data:
            return jsonify({"status": "error", "msg": "missing payload"}), 400
        store_event(data)
        return jsonify({"status": "ok"}), 201
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


@observer_bp.route("/sessions", methods=["GET"])
def list_sessions():
    """Return distinct session IDs with recorded events."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM events ORDER BY session_id"
        ).fetchall()
        conn.close()
        return jsonify({"sessions": [r["session_id"] for r in rows]})
    except Exception as e:
        return jsonify({"sessions": [], "error": str(e)}), 500


@observer_bp.route("/session/<session_id>", methods=["GET"])
def get_session(session_id: str):
    """Return the full ordered timeline for a session."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY turn ASC, id ASC",
            (session_id,),
        ).fetchall()
        conn.close()

        events = []
        for r in rows:
            events.append({
                "session_id": r["session_id"],
                "timestamp": r["timestamp"],
                "turn": r["turn"],
                "state": r["state"],
                "behavior": json.loads(r["behavior"]),
                "artifacts": json.loads(r["artifacts"]),
                "osint": json.loads(r["osint"]),
                "response": r["response"],
            })

        return jsonify({"session_id": session_id, "events": events})
    except Exception as e:
        return jsonify({"session_id": session_id, "events": [], "error": str(e)}), 500


@observer_bp.route("/health", methods=["GET"])
def observer_health():
    """Observer health check."""
    return jsonify({"status": "healthy", "service": "ANCHOR Observer Store"})


# ── Standalone entrypoint (for local testing only) ───────────────────────
if __name__ == "__main__":
    from flask import Flask
    import sys

    standalone_app = Flask(__name__)
    standalone_app.register_blueprint(observer_bp)
    init_observer_db()

    port = int(os.getenv("OBSERVER_PORT", "9090"))
    print(f"ANCHOR Observer Store (standalone) on http://0.0.0.0:{port}")
    print(f"  DB: {os.path.abspath(DB_PATH)}")
    standalone_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
