"""Command line entry points.

``flask setup`` provisions the four founding accounts and the initial projects.
It is safe to run repeatedly: existing accounts are reconciled to the official
record but never given a new password unless ``--rotate-passwords`` is passed.
"""

from __future__ import annotations

import logging
import secrets
import sys

import click
from flask import current_app
from flask.cli import with_appcontext

from app.extensions import db

log = logging.getLogger(__name__)


def register_cli(app) -> None:
    app.cli.add_command(setup)
    app.cli.add_command(create_ceo)
    app.cli.add_command(invite)
    app.cli.add_command(seed_dev)
    app.cli.add_command(create_user)
    app.cli.add_command(reset_password)
    app.cli.add_command(deactivate_user)
    app.cli.add_command(sync_config)
    app.cli.add_command(seed_demo)
    app.cli.add_command(run_maintenance)


def _banner(title: str) -> None:
    click.echo()
    click.echo("=" * 72)
    click.echo(f"  {title}")
    click.echo("=" * 72)


def _guard_production(allow: bool, action: str) -> None:
    if current_app.config.get("ENV_NAME") == "production" and not allow:
        click.echo(
            f"Refusing to {action} in production. "
            "Pass --yes-i-mean-it if you are certain.",
            err=True,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Initial setup
# ---------------------------------------------------------------------------

@click.command("setup")
@click.option(
    "--rotate-passwords",
    is_flag=True,
    help="Generate new passwords for accounts that already exist.",
)
@click.option("--no-projects", is_flag=True, help="Skip creating the initial projects.")
@click.option(
    "--yes-i-mean-it",
    is_flag=True,
    help="Allow this command to run against a production configuration.",
)
@with_appcontext
def setup(rotate_passwords: bool, no_projects: bool, yes_i_mean_it: bool) -> None:
    """Create the founding accounts and initial projects for Tavryc.

    Safe to rerun. Existing accounts keep their passwords unless you pass
    --rotate-passwords, and nothing is ever deleted.
    """
    _guard_production(yes_i_mean_it, "run initial setup")

    from app.services.seed_service import seed_founders
    from app.services.achievement_service import ensure_catalogue
    from app.services.level_service import ensure_ladder
    from app.services.xp_service import ensure_rules
    from app.services import settings_service

    _banner("Tavryc initial setup")

    db.create_all()
    click.echo("Schema present.")

    settings_service.ensure_defaults()
    ensure_ladder()
    ensure_rules()
    ensure_catalogue()
    click.echo("Configuration, level ladder, XP rules and achievements seeded.")

    result = seed_founders(
        rotate_passwords=rotate_passwords, create_projects=not no_projects
    )

    if result.created:
        _banner("INITIAL CREDENTIALS — SAVE THESE NOW")
        click.echo("These passwords are shown once and are not recoverable.")
        click.echo("Each founder should change theirs after the first sign-in.\n")
        for identifier, password in result.created:
            click.echo(f"  {identifier:<24} {password}")
        click.echo()
        click.echo("Deliver each password privately. Do not email them together.")
    else:
        click.echo("All founder accounts already existed; no passwords were changed.")

    if result.existing:
        click.echo(f"Existing accounts left untouched: {', '.join(result.existing)}")

    if not no_projects:
        if result.projects_created:
            click.echo(f"Projects created: {', '.join(result.projects_created)}")
        if result.projects_existing:
            click.echo(f"Projects already present: {', '.join(result.projects_existing)}")

    click.echo()
    click.echo("Next: run `flask sync-config` after changing any setting, then sign in.")


@click.command("sync-config")
@with_appcontext
def sync_config() -> None:
    """Seed or repair settings, levels, XP rules and achievements."""
    from app.services.achievement_service import ensure_catalogue
    from app.services.level_service import ensure_ladder
    from app.services.xp_service import ensure_rules
    from app.services import settings_service

    settings_service.ensure_defaults()
    ensure_ladder()
    ensure_rules()
    ensure_catalogue()
    click.echo("Configuration is up to date.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@click.command("create-ceo")
@click.option("--email", prompt=True, help="Login identifier for the CEO.")
@click.option("--username", prompt=True, help="Short username.")
@click.option("--full-name", prompt="Full name", help="Display name.")
@click.password_option("--password", help="Leave blank to have one generated.")
@with_appcontext
def create_ceo(email: str, username: str, full_name: str, password: str | None) -> None:
    """Create a CEO account. Used when the four founders are already set up."""
    from app.models.user import User, UserRole
    from app.services.crypto_service import hash_password, validate_password_strength
    from app.services.seed_service import generate_initial_password
    from app.services.achievement_service import ensure_catalogue, grant
    from app.services.level_service import ensure_ladder
    from app.services.xp_service import ensure_profile, ensure_rules
    from app.services.audit_service import record_audit

    existing_ceo = User.query.filter_by(role=UserRole.CEO.value).first()
    if existing_ceo is not None:
        click.echo(
            f"A CEO already exists ({existing_ceo.username}). "
            "Use `flask create-user --role CEO` if you intend to add another.",
            err=True,
        )
        sys.exit(1)

    if User.query.filter(
        (db.func.lower(User.email) == email.strip().lower())
        | (db.func.lower(User.username) == username.strip().lower())
    ).first():
        click.echo("That email or username is already in use.", err=True)
        sys.exit(1)

    generated = False
    if not password:
        password = generate_initial_password()
        generated = True
    try:
        validate_password_strength(password)
    except Exception as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    ensure_ladder()
    ensure_rules()
    ensure_catalogue()

    user = User(
        email=email.strip().lower(),
        username=username.strip(),
        full_name=full_name.strip(),
        role=UserRole.CEO.value,
        is_active_flag=True,
    )
    user.password_hash = hash_password(password)
    db.session.add(user)
    db.session.commit()
    ensure_profile(user)
    grant(user, "welcome_aboard")

    record_audit(
        "user.created",
        entity_type="user",
        entity_id=user.id,
        summary=f"CEO account created for {user.username}",
        after={"username": user.username, "role": user.role},
        user_id=None,
    )

    click.echo(f"CEO created: {user.username} <{user.email}>")
    if generated:
        _banner("GENERATED PASSWORD — SHOWN ONCE")
        click.echo(f"  {password}")


@click.command("create-user")
@click.option("--email", prompt=True)
@click.option("--username", prompt=True)
@click.option("--full-name", prompt="Full name")
@click.option("--role", type=click.Choice(["CEO", "MEMBER"]), default="MEMBER")
@click.option("--title", default=None, help="Official role title shown on the profile.")
@click.option("--password", default=None, help="Leave blank to have one generated.")
@with_appcontext
def create_user(email, username, full_name, role, title, password) -> None:
    """Create an account directly. The CEO or an administrator runs this."""
    from app.models.user import User
    from app.services.crypto_service import hash_password, validate_password_strength
    from app.services.seed_service import generate_initial_password
    from app.services.achievement_service import ensure_catalogue, grant
    from app.services.xp_service import ensure_profile, ensure_rules
    from app.services.level_service import ensure_ladder
    from app.services.audit_service import record_audit

    if User.query.filter(
        (db.func.lower(User.email) == email.strip().lower())
        | (db.func.lower(User.username) == username.strip().lower())
    ).first():
        click.echo("That email or username is already in use.", err=True)
        sys.exit(1)

    generated = False
    if not password:
        password = generate_initial_password()
        generated = True
    try:
        validate_password_strength(password)
    except Exception as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    ensure_ladder()
    ensure_rules()
    ensure_catalogue()

    user = User(
        email=email.strip().lower(),
        username=username.strip(),
        full_name=full_name.strip(),
        role=role,
        title=title,
        is_active_flag=True,
    )
    user.password_hash = hash_password(password)
    db.session.add(user)
    db.session.commit()
    ensure_profile(user)
    grant(user, "welcome_aboard")

    record_audit(
        "user.created",
        entity_type="user",
        entity_id=user.id,
        summary=f"Account created for {user.username} as {role}",
        after={"username": user.username, "role": role, "title": title},
        user_id=None,
    )
    click.echo(f"Account created: {user.username} <{user.email}> [{role}]")
    if generated:
        _banner("GENERATED PASSWORD — SHOWN ONCE")
        click.echo(f"  {password}")


@click.command("invite")
@click.argument("email")
@click.option("--role", type=click.Choice(["CEO", "MEMBER"]), default="MEMBER")
@with_appcontext
def invite(email: str, role: str) -> None:
    """Issue a single-use invitation link for a new member."""
    from app.models.user import User, UserRole
    from app.services.auth_service import create_invitation
    from app.services.mail_service import send_invitation_email
    from flask import url_for

    ceo = User.query.filter_by(role=UserRole.CEO.value).first()
    if ceo is None:
        click.echo("Create a CEO account first with `flask create-ceo`.", err=True)
        sys.exit(1)

    try:
        invitation, raw_token = create_invitation(email, role, ceo)
    except ValueError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    link = url_for("auth.accept_invite", token=raw_token, _external=True)
    sent = send_invitation_email(invitation, link)

    _banner("INVITATION LINK")
    click.echo(f"  Member:  {invitation.email}")
    click.echo(f"  Role:    {invitation.role}")
    click.echo(f"  Expires: {invitation.expires_at.isoformat()}")
    click.echo()
    click.echo(f"  {link}")
    click.echo()
    if sent:
        click.echo("The link was emailed.")
    else:
        click.echo(
            "SMTP is not configured, so the link was written to the application log "
            "instead. Deliver it to the member privately."
        )


@click.command("reset-password")
@click.argument("identifier")
@with_appcontext
def reset_password(identifier: str) -> None:
    """Generate a single-use reset link for a member."""
    from flask import url_for
    from app.services.auth_service import create_reset_token, find_user_by_identifier
    from app.services.mail_service import send_reset_email

    user = find_user_by_identifier(identifier)
    if user is None:
        click.echo("No account matches that identifier.", err=True)
        sys.exit(1)

    raw_token = create_reset_token(user)
    link = url_for("auth.reset_password", token=raw_token, _external=True)
    sent = send_reset_email(user, link)

    _banner("PASSWORD RESET LINK")
    click.echo(f"  Member: {user.display_name} <{user.email}>")
    click.echo()
    click.echo(f"  {link}")
    click.echo()
    click.echo("Emailed." if sent else "SMTP not configured; the link is in the log.")
    click.echo("The link expires in 60 minutes and can be used once.")


@click.command("deactivate-user")
@click.argument("identifier")
@click.option("--reactivate", is_flag=True, help="Reactivate instead of deactivating.")
@with_appcontext
def deactivate_user(identifier: str, reactivate: bool) -> None:
    """Deactivate or reactivate an account. Nothing is deleted."""
    from app.models.base import utcnow
    from app.services.auth_service import find_user_by_identifier
    from app.services.audit_service import record_audit

    user = find_user_by_identifier(identifier)
    if user is None:
        click.echo("No account matches that identifier.", err=True)
        sys.exit(1)

    if reactivate:
        user.is_active_flag = True
        user.deactivated_at = None
        action = "user.reactivated"
        message = f"{user.username} reactivated."
    else:
        user.is_active_flag = False
        user.deactivated_at = utcnow()
        action = "user.deactivated"
        message = f"{user.username} deactivated. Existing sessions are rejected on next request."
    db.session.commit()

    record_audit(
        action,
        entity_type="user",
        entity_id=user.id,
        summary=message,
        user_id=None,
    )
    click.echo(message)


# ---------------------------------------------------------------------------
# Development helpers
# ---------------------------------------------------------------------------

@click.command("seed-dev")
@click.option("--yes-i-mean-it", is_flag=True)
@with_appcontext
def seed_dev(yes_i_mean_it: bool) -> None:
    """Create the founder accounts plus example work. Never runs in production."""
    _guard_production(yes_i_mean_it, "seed development data")

    if current_app.config.get("ENV_NAME") == "production":
        click.echo("Refusing to seed development data in production.", err=True)
        sys.exit(1)

    from app.services.seed_service import seed_founders

    result = seed_founders(create_projects=True)
    if result.created:
        _banner("DEV CREDENTIALS")
        for identifier, password in result.created:
            click.echo(f"  {identifier:<24} {password}")
    else:
        click.echo("Founder accounts already exist.")
    click.echo("Development data ready.")


@click.command("seed-demo")
@click.option("--yes-i-mean-it", is_flag=True)
@with_appcontext
def seed_demo(yes_i_mean_it: bool) -> None:
    """Add clearly-labelled demonstration records. Never runs in production."""
    _guard_production(yes_i_mean_it, "seed demonstration data")

    from app.services.demo_service import seed_demo as _seed

    created = _seed()
    if not created:
        click.echo("Demo data was not created. See the log for the reason.")
        return
    click.echo("Demonstration records created and clearly labelled as such.")


@click.command("maintenance")
@with_appcontext
def run_maintenance() -> None:
    """Recompute streaks, refresh challenges and purge expired uploads.

    Runs on demand. Nothing in the application requires a scheduler: every value
    this command produces is also derived lazily on the request path, so the
    site works correctly if this is never run.
    """
    from app.models.user import User
    from app.services.challenge_service import ensure_weekly_challenges
    from app.services.streak_service import repair_streaks
    from app.services.upload_service import purge_expired
    from app.services.xp_service import recompute_profile

    users = User.query.filter_by(is_active_flag=True).all()
    for user in users:
        repair_streaks(user)
        recompute_profile(user)

    challenges = ensure_weekly_challenges()
    purged = purge_expired(older_than_days=30, actor=None)

    click.echo(f"Streaks and balances recomputed for {len(users)} member(s).")
    click.echo(f"Active weekly challenges: {len(challenges)}.")
    click.echo(f"Expired uploads purged: {purged}.")
