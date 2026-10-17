"""Founder account provisioning.

``seed_founders`` creates the four founding accounts exactly once each. It is
safe to rerun: an account that already exists is never overwritten, and its
password is never reset unless the operator explicitly asks for it.
"""

from __future__ import annotations

import logging
import secrets
import string

from app.extensions import db
from app.founders import FOUNDERS, INITIAL_PROJECTS
from app.models.base import utcnow
from app.models.project import Project, ProjectMember, ProjectRole, ProjectStatus
from app.models.user import User, UserRole
from app.services.audit_service import record_audit
from app.services.crypto_service import hash_password, validate_password_strength

log = logging.getLogger(__name__)

# Deliberately avoids characters that are easy to misread when transcribed from
# a terminal: 0/O, 1/l/I.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%^*_-+="
_PASSWORD_LENGTH = 20


def generate_initial_password() -> str:
    """Cryptographically strong password that satisfies the strength policy."""
    while True:
        candidate = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH))
        try:
            validate_password_strength(candidate)
        except Exception:
            continue
        return candidate


class SeedResult:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []      # (identifier, plaintext password)
        self.existing: list[str] = []
        self.projects_created: list[str] = []
        self.projects_existing: list[str] = []

    @property
    def created_any(self) -> bool:
        return bool(self.created)


def seed_founders(
    *,
    rotate_passwords: bool = False,
    create_projects: bool = True,
) -> SeedResult:
    """Create missing founder accounts and the initial projects.

    Parameters
    ----------
    rotate_passwords:
        When True, a new password is generated for accounts that already exist.
        This is off by default so rerunning setup never surprises a founder.
    create_projects:
        Also create Raasta, NotesVault and Internal Tools when absent.
    """
    result = SeedResult()

    for spec in FOUNDERS:
        user = _find_existing(spec)
        if user is None:
            password = generate_initial_password()
            user = _create_founder(spec, password)
            result.created.append((spec["identifier"], password))
        else:
            _reconcile_founder(spec, user)
            if rotate_passwords:
                password = generate_initial_password()
                user.password_hash = hash_password(password)
                db.session.commit()
                result.created.append((spec["identifier"], password))
                record_audit(
                    "founder.password_rotated",
                    entity_type="user",
                    entity_id=user.id,
                    summary=f"Initial password rotated for {user.identifier_label}",
                    user_id=None,
                )
            else:
                result.existing.append(spec["identifier"])

        _ensure_profile_records(user)

    if create_projects:
        _seed_projects(result)

    _link_all_projects_to_ceo()

    return result


def _find_existing(spec: dict) -> User | None:
    from sqlalchemy import func, or_

    return (
        User.query.filter(
            or_(
                func.lower(User.email) == spec["identifier"].lower(),
                func.lower(User.username) == spec["username"].lower(),
            )
        ).first()
    )


def _create_founder(spec: dict, password: str) -> User:
    user = User(
        email=spec["identifier"],
        username=spec["username"],
        full_name=spec["full_name"],
        title=spec["title"],
        role=spec["role"],
        bio=spec["summary"],
        timezone="Asia/Karachi",
        is_active_flag=True,
        must_change_password=False,
    )
    user.password_hash = hash_password(password)
    db.session.add(user)
    db.session.commit()

    record_audit(
        "founder.created",
        entity_type="user",
        entity_id=user.id,
        summary=f"Founder account created for {spec['full_name']} ({spec['identifier']})",
        after={
            "identifier": spec["identifier"],
            "role": spec["role"],
            "title": spec["title"],
        },
        user_id=None,
    )
    return user


def _reconcile_founder(spec: dict, user: User) -> None:
    """Bring an existing row in line with the official record, without touching
    the password or any member-authored field."""
    changed = False
    if user.full_name != spec["full_name"]:
        user.full_name = spec["full_name"]
        changed = True
    if user.role != spec["role"]:
        user.role = spec["role"]
        changed = True
    if user.title != spec["title"]:
        user.title = spec["title"]
        changed = True
    if not user.email:
        user.email = spec["identifier"]
        changed = True
    if changed:
        db.session.commit()
        record_audit(
            "founder.reconciled",
            entity_type="user",
            entity_id=user.id,
            summary=f"Founder record reconciled for {spec['identifier']}",
            user_id=None,
        )


def _ensure_profile_records(user: User) -> None:
    """Create the gamification rows this member needs. Idempotent."""
    from app.services.achievement_service import ensure_catalogue
    from app.services.xp_service import ensure_profile

    ensure_catalogue()
    ensure_profile(user)
    _grant_welcome(user)


def _grant_welcome(user: User) -> None:
    from app.services.achievement_service import grant

    grant(user, "welcome_aboard")


def _seed_projects(result: SeedResult) -> None:
    for spec in INITIAL_PROJECTS:
        existing = Project.query.filter_by(slug=spec["slug"]).first()
        if existing is not None:
            result.projects_existing.append(spec["name"])
            continue

        lead = User.query.filter_by(username=spec["lead"]).first()
        project = Project(
            name=spec["name"],
            slug=spec["slug"],
            description=spec["description"],
            status=spec["status"] if spec["status"] in ProjectStatus.values() else ProjectStatus.PLANNING.value,
            owner_id=lead.id if lead else None,
            created_by_id=lead.id if lead else None,
        )
        db.session.add(project)
        db.session.flush()

        member_ids: set[int] = set()
        if lead:
            db.session.add(
                ProjectMember(
                    project_id=project.id,
                    user_id=lead.id,
                    role_in_project=ProjectRole.LEAD.value,
                )
            )
            member_ids.add(lead.id)
        for username in spec["members"]:
            member = User.query.filter_by(username=username).first()
            if member and member.id not in member_ids:
                db.session.add(
                    ProjectMember(
                        project_id=project.id,
                        user_id=member.id,
                        role_in_project=ProjectRole.MEMBER.value,
                    )
                )
                member_ids.add(member.id)
        db.session.commit()
        result.projects_created.append(spec["name"])
        record_audit(
            "project.created",
            entity_type="project",
            entity_id=project.id,
            summary=f"Initial project {project.name} created during setup",
            user_id=None,
        )


def _link_all_projects_to_ceo() -> None:
    """The CEO oversees every project, including ones created later."""
    ceo = User.query.filter_by(role=UserRole.CEO.value).first()
    if ceo is None:
        return
    for project in Project.query.all():
        if ceo.id not in project.member_ids:
            db.session.add(
                ProjectMember(
                    project_id=project.id,
                    user_id=ceo.id,
                    role_in_project=ProjectRole.MEMBER.value,
                )
            )
    db.session.commit()


def _ensure_project_lead(project: Project, user: User) -> None:
    if user.id in project.member_ids:
        for membership in project.members:
            if membership.user_id == user.id:
                membership.role_in_project = ProjectRole.LEAD.value
                break
    else:
        db.session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role_in_project=ProjectRole.LEAD.value,
            )
        )
