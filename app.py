#!/usr/bin/env python3
"""
Among Us: Campus Edition
A lightweight Flask app for running a real-life Among Us game.

Built on the same core idea as the original Assassins CGI game:
a CSV/DB of players with codes, a kill-confirmation form, and an
auto-updating status page. This version adds tasks (photo or code
verification), a public death log, and an admin panel.
"""

import os
import random
import smtplib
import sqlite3
import time
import uuid
from datetime import datetime
from email.mime.text import MIMEText

from flask import (
    Flask, request, redirect, url_for, session, render_template,
    flash, send_from_directory, g
)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "game.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "private_uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "heic", "webp"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-before-deploying")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12MB per upload

# Default admin password - CHANGE THIS via env var ADMIN_PASSWORD before hosting
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact TEXT,
            login_code TEXT UNIQUE NOT NULL,
            kill_code TEXT UNIQUE NOT NULL,
            alive INTEGER NOT NULL DEFAULT 1,
            is_impostor INTEGER NOT NULL DEFAULT 0,
            kills INTEGER NOT NULL DEFAULT 0,
            last_kill_time REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_status (
            player_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'incomplete', -- incomplete / complete
            proof TEXT,                                -- filename or entered code
            timestamp TEXT,
            PRIMARY KEY (player_id, task_id),
            FOREIGN KEY (player_id) REFERENCES players(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS kill_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            victim_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Seed default settings if missing
    defaults = {
        "locations": "Library,Dining Hall,Quad,Gym,Dorm A,Dorm B,Student Union",
        "cooldown_minutes": "15",
        "kill_start_hour": "8",   # 24hr clock, kills blocked outside this window
        "kill_end_hour": "22",
        "game_name": "Among Us: Campus Edition",
    }
    for k, v in defaults.items():
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    db.commit()
    db.close()


def get_setting(key, default=None):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def generate_login_code(name):
    """name + a random 4-digit number, e.g. 'JimHalpert4821'."""
    stripped_name = "".join(ch for ch in name if ch.isalnum())
    return f"{stripped_name}{random.randint(1000, 9999)}"


# --------------------------------------------------------------------------
# Email sending
# --------------------------------------------------------------------------
# Configure these via environment variables before running a real game:
#   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, FROM_EMAIL
# If they're not set, the email is just printed to the console instead of
# sent -- handy for local testing without a real mail server.

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER or "no-reply@example.com")


def send_login_email(
    name,
    contact,
    login_code,
    kill_code,
    is_impostor,
):
    """Send a player all game information imported from the CSV.

    Returns True when the email is sent successfully.
    Returns False when SMTP is not configured or sending fails.
    """

    role_name = "Impostor" if is_impostor else "Civilian"

    subject = "Your Among Us game information"

    body = (
        f"Hi {name},\n\n"
        f"You have been registered for Among Us: Campus Edition.\n\n"
        f"Here is your private game information:\n\n"
        f"Role: {role_name}\n"
        f"Login code: {login_code}\n"
        f"Kill code: {kill_code}\n\n"
        f"Use your login code to enter the game website.\n\n"
        f"Your kill code must remain private. Do not send it to another "
        f"player or show it to anyone during the game. If you are eliminated "
        f"in person, give your kill code only to the player who eliminated "
        f"you so they can confirm the elimination on the website.\n\n"
    )

    if is_impostor:
        body += (
            f"You are an impostor.\n\n"
            f"Your objective is to eliminate civilians without being caught. "
            f"After a valid in-person elimination, enter the victim's kill "
            f"code into the game website.\n\n"
        )
    else:
        body += (
            f"You are a civilian.\n\n"
            f"Complete your assigned tasks, watch for suspicious behaviour, "
            f"and avoid revealing your kill code unless you have genuinely "
            f"been eliminated.\n\n"
        )

    body += (
        f"Keep this email private for the duration of the game.\n\n"
        f"Good luck,\n"
        f"Among Us Game Admin"
    )

    if not SMTP_HOST or not contact:
        print(
            "[email skipped -- no SMTP configured or no email address]\n"
            f"Recipient: {contact!r}\n"
            f"Subject: {subject}\n\n"
            f"{body}\n"
        )
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = contact

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()

            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)

            server.sendmail(
                FROM_EMAIL,
                [contact],
                msg.as_string(),
            )

        print(f"[email sent] {contact}")
        return True

    except Exception as error:
        print(f"[email failed] {contact}: {error}")
        return False


def current_player():
    pid = session.get("player_id")
    if not pid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()


def alive_count():
    db = get_db()
    row = db.execute("SELECT COUNT(*) c FROM players WHERE alive = 1").fetchone()
    return row["c"]


def total_count():
    db = get_db()
    row = db.execute("SELECT COUNT(*) c FROM players").fetchone()
    return row["c"]


# --------------------------------------------------------------------------
# Player-facing routes
# --------------------------------------------------------------------------

@app.route("/signup")
def signup():
    # Registration happens externally through the organiser's Google Form.
    # There is deliberately no self-signup flow inside the game app.
    flash("Player registration is handled through the organiser's Google Form.")
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        code = request.form.get("login_code", "").strip()
        db = get_db()
        player = db.execute(
            "SELECT * FROM players WHERE login_code = ?", (code,)
        ).fetchone()
        if player:
            session["player_id"] = player["id"]
            return redirect(url_for("dashboard"))
        flash("That login code wasn't recognised. Double check and try again.")
        return redirect(url_for("login"))

    if current_player():
        return redirect(url_for("dashboard"))

    return render_template("login.html", game_name=get_setting("game_name"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    player = current_player()
    if not player:
        return redirect(url_for("login"))

    db = get_db()

    tasks = db.execute(
        """
        SELECT t.id, t.description,
               COALESCE(ts.status, 'incomplete') AS status
        FROM tasks t
        LEFT JOIN task_status ts
          ON ts.task_id = t.id AND ts.player_id = ?
        ORDER BY t.id
        """,
        (player["id"],),
    ).fetchall()

    deaths = db.execute(
        """
        SELECT p.name, k.location, k.timestamp
        FROM kill_log k JOIN players p ON p.id = k.victim_id
        ORDER BY k.id DESC
        """
    ).fetchall()

    locations = get_setting("locations").split(",")

    return render_template(
        "dashboard.html",
        player=player,
        tasks=tasks,
        deaths=deaths,
        alive=alive_count(),
        total=total_count(),
        locations=locations,
        game_name=get_setting("game_name"),
    )


@app.route("/kill", methods=["POST"])
def kill():
    player = current_player()
    if not player:
        return redirect(url_for("login"))

    if not player["is_impostor"]:
        flash("Only the impostor can enter kills.")
        return redirect(url_for("dashboard"))

    if not player["alive"]:
        flash("You're dead — you can't do that anymore.")
        return redirect(url_for("dashboard"))

    killcode = request.form.get("target_killcode", "").strip()
    location = request.form.get("location", "").strip()
    db = get_db()

    # Cooldown check
    cooldown = int(get_setting("cooldown_minutes", "15")) * 60
    now = time.time()
    if now - (player["last_kill_time"] or 0) < cooldown:
        wait = int((cooldown - (now - (player["last_kill_time"] or 0))) / 60) + 1
        flash(f"Cooldown active. Wait about {wait} more minute(s) before killing again.")
        return redirect(url_for("dashboard"))

    # Time-of-day window check
    hour = datetime.now().hour
    start_h = int(get_setting("kill_start_hour", "8"))
    end_h = int(get_setting("kill_end_hour", "22"))
    if not (start_h <= hour < end_h):
        flash(f"Kills are only allowed between {start_h}:00 and {end_h}:00.")
        return redirect(url_for("dashboard"))

    target = db.execute(
        "SELECT * FROM players WHERE kill_code = ? AND alive = 1", (killcode,)
    ).fetchone()

    if not target:
        flash("No living player matches that kill code.")
        return redirect(url_for("dashboard"))

    if target["id"] == player["id"]:
        flash("You can't kill yourself.")
        return redirect(url_for("dashboard"))

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE players SET alive = 0 WHERE id = ?", (target["id"],))
    db.execute(
        "UPDATE players SET kills = kills + 1, last_kill_time = ? WHERE id = ?",
        (now, player["id"]),
    )
    db.execute(
        "INSERT INTO kill_log (victim_id, location, timestamp) VALUES (?, ?, ?)",
        (target["id"], location or "Unknown", timestamp),
    )
    db.commit()

    flash(f"Kill confirmed. {target['name']} has been eliminated.")
    return redirect(url_for("dashboard"))


@app.route("/task/<int:task_id>/complete", methods=["POST"])
def complete_task(task_id):
    player = current_player()
    if not player:
        return redirect(url_for("login"))

    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        flash("Task not found.")
        return redirect(url_for("dashboard"))

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    file = request.files.get("photo")
    if not file or file.filename == "":
        flash("Please attach a photo.")
        return redirect(url_for("dashboard"))
    if not allowed_file(file.filename):
        flash("That file type isn't supported. Use jpg/png/heic/webp.")
        return redirect(url_for("dashboard"))
    ext = file.filename.rsplit(".", 1)[1].lower()
    fname = f"{player['id']}_{task_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(fname)))
    proof = fname

    db.execute(
        """
        INSERT INTO task_status (player_id, task_id, status, proof, timestamp)
        VALUES (?, ?, 'complete', ?, ?)
        ON CONFLICT(player_id, task_id)
        DO UPDATE SET status = 'complete', proof = excluded.proof, timestamp = excluded.timestamp
        """,
        (player["id"], task_id, proof, timestamp),
    )
    db.commit()
    flash("Task marked complete!")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Admin routes
# --------------------------------------------------------------------------

def admin_required():
    return session.get("is_admin", False)


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Wrong password.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
    tasks = db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    kills = db.execute(
        """
        SELECT p.name, k.location, k.timestamp
        FROM kill_log k JOIN players p ON p.id = k.victim_id
        ORDER BY k.id DESC
        """
    ).fetchall()
    submissions = db.execute(
        """
        SELECT p.name AS player_name, t.description,
               ts.proof, ts.timestamp
        FROM task_status ts
        JOIN players p ON p.id = ts.player_id
        JOIN tasks t ON t.id = ts.task_id
        WHERE ts.status = 'complete'
        ORDER BY ts.timestamp DESC
        """
    ).fetchall()
    settings_row = {
        k: get_setting(k)
        for k in [
            "locations", "cooldown_minutes", "kill_start_hour",
            "kill_end_hour", "game_name",
        ]
    }
    return render_template(
        "admin_dashboard.html",
        players=players,
        tasks=tasks,
        kills=kills,
        submissions=submissions,
        settings=settings_row,
        alive=alive_count(),
        total=total_count(),
    )


@app.route("/admin/assign_impostors", methods=["POST"])
def admin_assign_impostors():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    try:
        count = int(request.form.get("num_impostors", "1"))
    except ValueError:
        count = 1

    all_ids = [row["id"] for row in db.execute("SELECT id FROM players").fetchall()]
    if count > len(all_ids):
        flash(f"Only {len(all_ids)} player(s) signed up -- can't make {count} impostors.")
        return redirect(url_for("admin_dashboard"))

    db.execute("UPDATE players SET is_impostor = 0")
    chosen = random.sample(all_ids, count)
    for pid in chosen:
        db.execute("UPDATE players SET is_impostor = 1 WHERE id = ?", (pid,))
    db.commit()
    flash(f"Assigned {count} impostor(s) at random.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/players/add", methods=["POST"])
def admin_add_player():
    if not admin_required():
        return redirect(url_for("admin_login"))

    db = get_db()

    name = request.form.get("name", "").strip()
    contact = request.form.get("contact", "").strip()
    login_code = request.form.get("login_code", "").strip()
    kill_code = request.form.get("kill_code", "").strip()
    is_impostor = (
        1 if request.form.get("is_impostor") == "on" else 0
    )

    if not name:
        flash("Enter a player name.")
        return redirect(url_for("admin_dashboard"))

    if not contact or "@" not in contact:
        flash("Enter a valid email address.")
        return redirect(url_for("admin_dashboard"))

    if not login_code:
        flash("Enter a login code.")
        return redirect(url_for("admin_dashboard"))

    if not kill_code:
        flash("Enter a kill code.")
        return redirect(url_for("admin_dashboard"))

    try:
        db.execute(
            """
            INSERT INTO players (
                name,
                contact,
                login_code,
                kill_code,
                is_impostor
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                contact,
                login_code,
                kill_code,
                is_impostor,
            ),
        )

        db.commit()

        sent = send_login_email(
            name=name,
            contact=contact,
            login_code=login_code,
            kill_code=kill_code,
            is_impostor=is_impostor,
        )

        if sent:
            flash(f"Added {name} and sent their game email.")
        else:
            flash(
                f"Added {name}, but the email was not sent. "
                f"Check your SMTP configuration and terminal output."
            )

    except sqlite3.IntegrityError:
        db.rollback()
        flash(
            "That email, login code, or kill code is already being used."
        )

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/players/bulk_import", methods=["POST"])
def admin_bulk_import():
    """Import players and their game information from a CSV.

    Required columns:
        name
        email
        login_code
        kill_code
        impostor

    The importer uses the exact values entered by the organiser.
    It does not generate or randomly assign any codes or roles.
    """

    if not admin_required():
        return redirect(url_for("admin_login"))

    file = request.files.get("csv_file")

    if not file or file.filename == "":
        flash("Please choose a CSV file.")
        return redirect(url_for("admin_dashboard"))

    import csv
    import io

    try:
        content = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(
            "The CSV could not be read. Download it from Google Sheets "
            "using File → Download → Comma-separated values."
        )
        return redirect(url_for("admin_dashboard"))

    reader = csv.DictReader(io.StringIO(content))

    if not reader.fieldnames:
        flash("The uploaded CSV does not contain a header row.")
        return redirect(url_for("admin_dashboard"))

    normalised_headers = {
        (header or "").strip().lower().replace(" ", "_")
        for header in reader.fieldnames
    }

    required_headers = {
        "name",
        "email",
        "login_code",
        "kill_code",
        "impostor",
    }

    missing_headers = required_headers - normalised_headers

    if missing_headers:
        missing_text = ", ".join(sorted(missing_headers))
        flash(f"The CSV is missing required columns: {missing_text}.")
        return redirect(url_for("admin_dashboard"))

    db = get_db()

    added = 0
    emails_sent = 0
    email_failures = 0
    skipped = 0
    errors = []

    def normalise_row(raw_row):
        return {
            (key or "").strip().lower().replace(" ", "_"):
            (value or "").strip()
            for key, value in raw_row.items()
        }

    for row_number, raw_row in enumerate(reader, start=2):
        row = normalise_row(raw_row)

        name = row.get("name", "")
        contact = row.get("email", "")
        login_code = row.get("login_code", "")
        kill_code = row.get("kill_code", "")
        impostor_value = row.get("impostor", "")

        if not name:
            errors.append(f"Row {row_number}: name is missing.")
            skipped += 1
            continue

        if not contact or "@" not in contact:
            errors.append(
                f"Row {row_number}: email address is missing or invalid."
            )
            skipped += 1
            continue

        if not login_code:
            errors.append(f"Row {row_number}: login code is missing.")
            skipped += 1
            continue

        if not kill_code:
            errors.append(f"Row {row_number}: kill code is missing.")
            skipped += 1
            continue

        if impostor_value not in {"0", "1"}:
            errors.append(
                f"Row {row_number}: impostor must be either 0 or 1."
            )
            skipped += 1
            continue

        is_impostor = int(impostor_value)

        existing_email = db.execute(
            """
            SELECT id
            FROM players
            WHERE lower(contact) = lower(?)
            """,
            (contact,),
        ).fetchone()

        if existing_email:
            errors.append(
                f"Row {row_number}: {contact} is already registered."
            )
            skipped += 1
            continue

        existing_login = db.execute(
            """
            SELECT id
            FROM players
            WHERE login_code = ?
            """,
            (login_code,),
        ).fetchone()

        if existing_login:
            errors.append(
                f"Row {row_number}: login code {login_code} is already used."
            )
            skipped += 1
            continue

        existing_kill = db.execute(
            """
            SELECT id
            FROM players
            WHERE kill_code = ?
            """,
            (kill_code,),
        ).fetchone()

        if existing_kill:
            errors.append(
                f"Row {row_number}: kill code {kill_code} is already used."
            )
            skipped += 1
            continue

        try:
            cursor = db.execute(
                """
                INSERT INTO players (
                    name,
                    contact,
                    login_code,
                    kill_code,
                    is_impostor
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    contact,
                    login_code,
                    kill_code,
                    is_impostor,
                ),
            )

            db.commit()

            email_sent = send_login_email(
                name=name,
                contact=contact,
                login_code=login_code,
                kill_code=kill_code,
                is_impostor=is_impostor,
            )

            added += 1

            if email_sent:
                emails_sent += 1
            else:
                email_failures += 1

        except sqlite3.IntegrityError as error:
            db.rollback()
            errors.append(
                f"Row {row_number}: database rejected the player: {error}"
            )
            skipped += 1

    flash(
        f"Import complete. Added {added} player(s), sent "
        f"{emails_sent} email(s), had {email_failures} email failure(s), "
        f"and skipped {skipped} row(s)."
    )

    for error in errors[:10]:
        flash(error)

    if len(errors) > 10:
        flash(
            f"There were {len(errors) - 10} additional row errors. "
            f"Check the CSV and the terminal output."
        )

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/players/<int:player_id>/delete", methods=["POST"])
def admin_delete_player(player_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute("DELETE FROM players WHERE id = ?", (player_id,))
    db.execute("DELETE FROM task_status WHERE player_id = ?", (player_id,))
    db.commit()
    flash("Player removed.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/tasks/add", methods=["POST"])
def admin_add_task():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    description = request.form.get("description", "").strip()
    db.execute("INSERT INTO tasks (description) VALUES (?)", (description,))
    db.commit()
    flash("Task added.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/tasks/<int:task_id>/delete", methods=["POST"])
def admin_delete_task(task_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.execute("DELETE FROM task_status WHERE task_id = ?", (task_id,))
    db.commit()
    flash("Task removed.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/settings", methods=["POST"])
def admin_settings():
    if not admin_required():
        return redirect(url_for("admin_login"))
    for key in ["locations", "cooldown_minutes", "kill_start_hour", "kill_end_hour", "game_name"]:
        val = request.form.get(key)
        if val is not None:
            set_setting(key, val.strip())
    flash("Settings updated.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/eject", methods=["POST"])
def admin_eject():
    """Manually mark a player dead — used for nightly vote ejections."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    player_id = request.form.get("player_id")
    location = "Ejected (vote)"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE players SET alive = 0 WHERE id = ?", (player_id,))
    db.execute(
        "INSERT INTO kill_log (victim_id, location, timestamp) VALUES (?, ?, ?)",
        (player_id, location, timestamp),
    )
    db.commit()
    flash("Player ejected.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    """Wipes players, tasks, task submissions, and the kill/eject log so
    you can test a fresh game. Settings (locations, cooldown, etc.) are
    kept as-is."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute("DELETE FROM task_status")
    db.execute("DELETE FROM kill_log")
    db.execute("DELETE FROM tasks")
    db.execute("DELETE FROM players")
    db.commit()
    session.clear()
    session["is_admin"] = True
    flash("Game reset. All players, tasks, and logs cleared.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/uploads/<filename>")
def uploaded_file(filename):
    if not admin_required():
        flash("Admin access is required to view submitted photos.")
        return redirect(url_for("admin_login"))
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# --------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        init_db()
    else:
        init_db()  # safe: uses IF NOT EXISTS / INSERT OR IGNORE
    app.run(debug=True, host="0.0.0.0", port=5000)
else:
    # Also init when imported by a WSGI server (e.g. PythonAnywhere)
    init_db()
