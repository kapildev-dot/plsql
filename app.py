"""Quiz Arena — Flask + Oracle + PL/SQL backend.

Expected Oracle objects:
  * insert_quiz_result(name, category, correct, total, points, time_spent,
                       status OUT, grade OUT, new_id OUT)
  * get_player_rank(student_id) RETURN NUMBER
  * get_leaderboard(limit, ref_cursor OUT)

The game store is in memory, so run one Flask worker for this version. For
multi-worker/production deployments, move game state to Redis or a database.
"""
from __future__ import annotations

import logging
import os
import random
import secrets
import threading
import time
from typing import Any

import oracledb
from flask import Flask, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-this-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=16 * 1024,  # API requests should be tiny
    # always serve fresh style.css / script.js while developing
    SEND_FILE_MAX_AGE_DEFAULT=0,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("quiz_arena")

DB_USER = os.getenv("DB_USER", "system")
DB_PASSWORD = os.getenv("DB_PASSWORD", "oracle")
DB_DSN = os.getenv("DB_DSN", "localhost:1521/XEPDB1")

TIME_LIMIT = 15
GRACE = 1.5
MIXED_ROUND_SIZE = 10
BASE_POINTS = {"easy": 100, "medium": 150, "hard": 200}
TIME_BONUS_MAX = 50
STREAK_STEP = 20
STREAK_CAP = 5
GAME_TTL = 60 * 60


def _q(qid: int, category: str, difficulty: str, question: str,
       options: list[str], answer: int) -> dict[str, Any]:
    if len(options) < 2 or not 0 <= answer < len(options):
        raise ValueError(f"Invalid question definition: {qid}")
    return {
        "id": qid,
        "category": category,
        "difficulty": difficulty,
        "question": question,
        "options": options,
        "answer": answer,
    }


QUESTIONS = [
    # Python
    _q(1, "Python", "easy", "Which keyword is used to define a function in Python?",
       ["func", "def", "function", "define"], 1),
    _q(2, "Python", "easy", "Which of these data types is immutable?",
       ["list", "dict", "tuple", "set"], 2),
    _q(3, "Python", "easy", "What does range(3) generate?",
       ["1, 2, 3", "0, 1, 2", "0, 1, 2, 3", "1, 2"], 1),
    _q(4, "Python", "medium", "Which block runs when no exception occurs in a try statement?",
       ["finally", "else", "except", "catch"], 1),
    _q(5, "Python", "medium", "What does [x * x for x in range(4)] return?",
       ["[0, 1, 4, 9]", "[1, 4, 9, 16]", "[0, 1, 2, 3]", "[1, 2, 3, 4]"], 0),
    # SQL
    _q(6, "SQL", "easy", "Which SQL command retrieves data from a table?",
       ["GET", "FETCH", "SELECT", "OPEN"], 2),
    _q(7, "SQL", "easy", "Which constraint uniquely identifies each record in a table?",
       ["FOREIGN KEY", "PRIMARY KEY", "NOT NULL", "CHECK"], 1),
    _q(8, "SQL", "medium", "Which clause filters groups after GROUP BY?",
       ["WHERE", "ORDER BY", "HAVING", "LIMIT"], 2),
    _q(9, "SQL", "medium", "Which JOIN returns all rows from the left table and matching rows from the right?",
       ["INNER JOIN", "LEFT JOIN", "CROSS JOIN", "SELF JOIN"], 1),
    _q(10, "SQL", "hard", "Which statement removes all rows but keeps the table structure?",
       ["DELETE", "DROP", "TRUNCATE", "REMOVE"], 2),
    # PL/SQL
    _q(11, "PL/SQL", "easy", "Which keyword terminates a PL/SQL block?",
       ["STOP;", "EXIT;", "TERMINATE;", "END;"], 3),
    _q(12, "PL/SQL", "easy", "Which section of a PL/SQL block is used to declare variables?",
       ["BEGIN", "DECLARE", "EXCEPTION", "END"], 1),
    _q(13, "PL/SQL", "medium", "Which built-in package is commonly used to print PL/SQL output?",
       ["UTL_FILE", "DBMS_SQL", "DBMS_JOB", "DBMS_OUTPUT"], 3),
    _q(14, "PL/SQL", "medium", "A database trigger normally runs automatically when:",
       ["It is called manually", "A DML event occurs", "Only a SELECT runs", "The server restarts"], 1),
    _q(15, "PL/SQL", "hard", "Which parameter mode lets a procedure return a value to its caller?",
       ["IN", "READ", "OUT", "CONST"], 2),
    # Web
    _q(16, "Web", "easy", "What does HTML stand for?",
       ["HyperText Markup Language", "High Text Machine Language",
        "Hyper Tool Multi Language", "Home Text Markup Language"], 0),
    _q(17, "Web", "easy", "Which CSS property changes text colour?",
       ["font-color", "text-color", "fgcolor", "color"], 3),
    _q(18, "Web", "easy", "Which HTML tag creates the largest heading?",
       ["<h6>", "<h1>", "<head>", "<title>"], 1),
    _q(19, "Web", "medium", "Which HTTP method is normally used to submit data?",
       ["GET", "HEAD", "POST", "TRACE"], 2),
    _q(20, "Web", "hard", "In CSS Flexbox, which property aligns items along the main axis?",
       ["align-items", "justify-content", "flex-wrap", "place-self"], 1),
]

QUESTION_MAP = {question["id"]: question for question in QUESTIONS}
CATEGORIES = ["Python", "SQL", "PL/SQL", "Web"]

# ---------------------------------------------------------------------------
# Server-side game store
# ---------------------------------------------------------------------------
GAMES: dict[str, dict[str, Any]] = {}
GAMES_LOCK = threading.RLock()


def create_game(game: dict[str, Any]) -> str:
    """Register a game and associate its random ID with this browser session."""
    now = time.time()
    game["lock"] = threading.RLock()
    game["touched"] = now
    game_id = secrets.token_urlsafe(24)

    with GAMES_LOCK:
        expired = [key for key, value in GAMES.items()
                   if now - value.get("touched", now) > GAME_TTL]
        for key in expired:
            GAMES.pop(key, None)
        GAMES[game_id] = game

    session["gid"] = game_id
    return game_id


def get_game() -> dict[str, Any] | None:
    game_id = session.get("gid")
    if not game_id:
        return None

    with GAMES_LOCK:
        game = GAMES.get(game_id)
        if game is not None:
            game["touched"] = time.time()
        return game


# ---------------------------------------------------------------------------
# Oracle connection pool and database helpers
# ---------------------------------------------------------------------------
_pool = None
_pool_lock = threading.Lock()


def get_conn():
    """Create the connection pool only when a database operation is requested."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = oracledb.create_pool(
                    user=DB_USER,
                    password=DB_PASSWORD,
                    dsn=DB_DSN,
                    min=1,
                    max=5,
                    increment=1,
                )
    return _pool.acquire()


def read_dbms_output(cursor) -> str:
    """Return the last line written to DBMS_OUTPUT, if any."""
    line_var = cursor.var(str, size=32767)
    status_var = cursor.var(int)
    lines: list[str] = []

    while True:
        cursor.callproc("DBMS_OUTPUT.GET_LINE", (line_var, status_var))
        if status_var.getvalue() != 0:
            break
        value = line_var.getvalue()
        if value:
            lines.append(value)

    return lines[-1] if lines else "No trigger output"


def save_result(game: dict[str, Any]) -> dict[str, Any]:
    """Save one completed game using the project's Oracle PL/SQL API."""
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.callproc("DBMS_OUTPUT.ENABLE", (None,))
            status_var = cursor.var(str, size=100)
            grade_var = cursor.var(str, size=100)
            id_var = cursor.var(int)

            cursor.callproc("insert_quiz_result", [
                game["name"],
                game["category"],
                game["correct"],
                len(game["qids"]),
                game["points"],
                round(game["time_spent"], 1),
                status_var,
                grade_var,
                id_var,
            ])
            trigger_message = read_dbms_output(cursor)
            student_id_value = id_var.getvalue()
            if student_id_value is None:
                raise RuntimeError(
                    "insert_quiz_result did not return an ID. Check its OUT parameter."
                )
            student_id = int(student_id_value)

            # Rank is queried before commit; the procedure's insert is visible here.
            rank = cursor.callfunc("get_player_rank", int, [student_id])

        conn.commit()
        return {
            "id": student_id,
            "status": status_var.getvalue(),
            "grade": grade_var.getvalue(),
            "message": trigger_message,
            "rank": rank,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    """Read leaderboard rows from get_leaderboard(limit, SYS_REFCURSOR)."""
    limit = max(1, min(int(limit), 100))
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            # 1. Initialize an oracledb cursor variable for the OUT parameter
            p_cursor = cursor.var(oracledb.CURSOR)

            # 2. Pass the variable to the stored procedure
            cursor.callproc("get_leaderboard", [limit, p_cursor])

            # 3. Extract the actual cursor object to fetch rows
            ref_cursor = p_cursor.getvalue()
            rows = ref_cursor.fetchall()

            return [
                {
                    "rank": row[0],
                    "id": row[1],
                    "name": row[2],
                    "category": row[3],
                    "points": row[4],
                    "correct": row[5],
                    "total": row[6],
                    "status": row[7],
                }
                for row in rows
            ]
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Helpers and error handling
# ---------------------------------------------------------------------------


def api_error(message: str, status_code: int = 400):
    return jsonify({"success": False, "error": message}), status_code


def current_question(game: dict[str, Any]) -> dict[str, Any]:
    return QUESTION_MAP[game["qids"][game["idx"]]]


def build_summary(game: dict[str, Any]) -> dict[str, Any]:
    total = len(game["qids"])
    return {
        "name": game["name"],
        "category": game["category"],
        "correct": game["correct"],
        "total": total,
        "points": game["points"],
        "accuracy": round(game["correct"] * 100 / total) if total else 0,
        "best_streak": game["best_streak"],
        "time_spent": round(game["time_spent"], 1),
    }


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unhandled application error")
    # Avoid exposing credentials, SQL, or server internals to the browser.
    return jsonify({"success": False, "error": "An unexpected server error occurred."}), 500

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/health")
def health():
    """App-only health check; it does not require Oracle to be online."""
    return jsonify({"success": True, "service": "Quiz Arena"})


@app.get("/api/categories")
def categories():
    items = [{"name": "Mixed", "count": min(MIXED_ROUND_SIZE, len(QUESTIONS))}]
    for category in CATEGORIES:
        count = sum(
            1 for question in QUESTIONS if question["category"] == category)
        items.append({"name": category, "count": count})
    return jsonify({"categories": items, "time_limit": TIME_LIMIT})


@app.get("/api/leaderboard")
def leaderboard():
    try:
        return jsonify({"success": True, "leaderboard": fetch_leaderboard(10)})
    except Exception as exc:
        logger.exception("Unable to load leaderboard")
        return jsonify({
            "success": False,
            "leaderboard": [],
            "error": "Leaderboard is temporarily unavailable. Check Oracle and PL/SQL objects.",
        }), 503


@app.post("/api/start")
def start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error("Send a valid JSON object with name and category.")

    raw_name = data.get("name", "")
    if not isinstance(raw_name, str):
        return api_error("Name must be text.")
    name = "".join(
        char for char in raw_name if char.isprintable()).strip()[:30]
    category = data.get("category") or "Mixed"

    if len(name) < 2:
        return api_error("Please enter a name (at least 2 characters).")
    if not isinstance(category, str):
        return api_error("Unknown category.")

    if category == "Mixed":
        pool = QUESTIONS
        size = min(MIXED_ROUND_SIZE, len(pool))
    elif category in CATEGORIES:
        pool = [question for question in QUESTIONS if question["category"] == category]
        size = len(pool)
    else:
        return api_error("Unknown category.")

    chosen = random.sample(pool, min(size, len(pool)))
    create_game({
        "name": name,
        "category": category,
        "qids": [question["id"] for question in chosen],
        "idx": 0,
        "correct": 0,
        "points": 0,
        "streak": 0,
        "best_streak": 0,
        "time_spent": 0.0,
        "q_start": None,
        "perm": None,
        "hidden": [],
        "fifty_used": False,
        "db": None,
    })
    return jsonify({"success": True, "total": len(chosen)})


@app.get("/api/question")
def question():
    game = get_game()
    if not game:
        return api_error("No active game. Please start a new quiz.", 409)

    with game["lock"]:
        if game["idx"] >= len(game["qids"]):
            return jsonify({"finished": True})

        question_data = current_question(game)
        # A refresh reuses the existing start time and option order.
        if game["q_start"] is None:
            game["q_start"] = time.monotonic()
            game["perm"] = random.sample(
                range(len(question_data["options"])), len(
                    question_data["options"])
            )
            game["hidden"] = []

        elapsed = max(0.0, time.monotonic() - game["q_start"])
        return jsonify({
            "finished": False,
            "name": game["name"],
            "number": game["idx"] + 1,
            "total": len(game["qids"]),
            "question": question_data["question"],
            "options": [question_data["options"][i] for i in game["perm"]],
            "difficulty": question_data["difficulty"],
            "category": question_data["category"],
            "time_limit": TIME_LIMIT,
            "time_left": max(0.0, TIME_LIMIT - elapsed),
            "points": game["points"],
            "streak": game["streak"],
            "correct": game["correct"],
            "fifty_available": not game["fifty_used"],
            "hidden": game["hidden"],
        })


@app.post("/api/fifty")
def fifty():
    game = get_game()
    if not game:
        return api_error("No active game. Please start a new quiz.", 409)

    with game["lock"]:
        if game["q_start"] is None or game["idx"] >= len(game["qids"]):
            return api_error("No active question.", 409)
        elapsed = time.monotonic() - game["q_start"]
        if elapsed > TIME_LIMIT + GRACE:
            return api_error("Time has expired for this question.", 409)
        if game["fifty_used"]:
            return api_error("50:50 has already been used in this game.", 409)

        question_data = current_question(game)
        wrong_positions = [
            position for position, original_index in enumerate(game["perm"])
            if original_index != question_data["answer"]
        ]
        game["hidden"] = random.sample(
            wrong_positions, min(2, len(wrong_positions)))
        game["fifty_used"] = True
        return jsonify({"success": True, "hidden": game["hidden"]})


@app.post("/api/answer")
def answer():
    game = get_game()
    if not game:
        return api_error("No active game. Please start a new quiz.", 409)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return api_error("Send a valid JSON object containing selected.")

    with game["lock"]:
        if game["idx"] >= len(game["qids"]):
            return api_error("There are no unanswered questions.", 409)
        if game["q_start"] is None or game["perm"] is None:
            return api_error("Question has not been issued yet.", 409)

        question_data = current_question(game)
        elapsed = max(0.0, time.monotonic() - game["q_start"])
        selected = data.get("selected")
        timed_out = elapsed > TIME_LIMIT + GRACE
        valid = (
            isinstance(selected, int)
            and not isinstance(selected, bool)
            and 0 <= selected < len(question_data["options"])
            and selected not in game["hidden"]
        )
        if timed_out or not valid:
            selected = None

        correct_position = game["perm"].index(question_data["answer"])
        is_correct = (
            selected is not None
            and game["perm"][selected] == question_data["answer"]
        )

        breakdown = {"base": 0, "time": 0, "streak": 0}
        if is_correct:
            game["streak"] += 1
            game["correct"] += 1
            game["best_streak"] = max(game["best_streak"], game["streak"])
            breakdown["base"] = BASE_POINTS[question_data["difficulty"]]
            breakdown["time"] = max(
                0,
                round(TIME_BONUS_MAX * (1 - min(elapsed, TIME_LIMIT) / TIME_LIMIT)),
            )
            breakdown["streak"] = min(
                game["streak"] - 1, STREAK_CAP) * STREAK_STEP
        else:
            game["streak"] = 0

        earned = sum(breakdown.values())
        game["points"] += earned
        game["time_spent"] += min(elapsed, TIME_LIMIT)

        # Consume the question once; a repeated request cannot score it again.
        game["idx"] += 1
        game["q_start"] = None
        game["perm"] = None
        game["hidden"] = []

        return jsonify({
            "correct": is_correct,
            "timed_out": timed_out or data.get("selected") is None,
            "correct_index": correct_position,
            "earned": earned,
            "breakdown": breakdown,
            "points": game["points"],
            "streak": game["streak"],
            "correct_total": game["correct"],
            "finished": game["idx"] >= len(game["qids"]),
        })


@app.post("/api/finish")
def finish():
    game = get_game()
    if not game:
        return api_error("No active game. Please start a new quiz.", 409)

    with game["lock"]:
        if game["idx"] < len(game["qids"]):
            return api_error("Quiz is not finished yet.", 409)

        summary = build_summary(game)
        if game["db"] is None:
            try:
                game["db"] = save_result(game)
            except Exception:
                logger.exception("Failed to save quiz result to Oracle")
                return jsonify({
                    "success": False,
                    "error": "Could not save the result. Check Oracle connectivity and PL/SQL procedure signatures.",
                    "summary": summary,
                }), 503
        db_result = game["db"]

    try:
        board = fetch_leaderboard(10)
    except Exception:
        logger.exception("Result saved, but leaderboard could not be loaded")
        board = []

    return jsonify({
        "success": True,
        "summary": summary,
        "db": db_result,
        "leaderboard": board,
    })


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    # Debug is off by default because the development debugger is unsafe to expose.
    app.run(host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "5000")), debug=debug)
