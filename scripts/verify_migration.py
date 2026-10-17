"""Verify a database built by ``flask db upgrade`` contains the required schema."""
import sqlite3
import sys

path = sys.argv[1]
con = sqlite3.connect(path)
tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print("tables:", len(tables))

required = [
    "users", "tasks", "focus_sessions", "chat_rooms", "decision_records", "streaks",
    "user_xp_summary", "suggestion_comments", "suggestion_status_events",
    "suggestion_anon_votes", "lounge_read_state", "xp_transactions", "audit_log",
    "notifications", "settings", "lounge_posts", "submissions", "reviews", "attachments",
    "projects", "project_members", "milestones", "blockers", "daily_checkins",
    "daily_reports", "daily_report_revisions", "workdays", "workday_exceptions",
    "knowledge_entries", "announcements", "announcement_reads", "invitations",
    "password_reset_tokens", "login_attempts", "achievements", "member_achievements",
    "member_badges", "challenges", "challenge_progress", "level_definitions", "xp_rules",
    "xp_profiles", "suggestions", "suggestion_votes", "subtask" "s", "task_comments",
]
missing = [name for name in required if name not in tables]
print("missing:", missing)

focus_cols = {row[1] for row in con.execute("PRAGMA table_info(focus_sessions)")}
print("focus guards present:", {"open_key", "credit_key"} <= focus_cols)

indexes = {row[1] for row in con.execute("PRAGMA index_list(focus_sessions)")}
print("guard indexes:", sorted(i for i in indexes if "key" in i))
