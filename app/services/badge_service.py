"""CEO-granted recognition: custom badges and titles.

These are deliberately separate from system achievements:

* A system achievement is earned by meeting a rule and can only be granted by
  ``achievement_service``.
* A recognition badge is a human decision by the CEO and is always labelled as
  recognition on the profile. It never implies a formal company appointment —
  ``is_formal_appointment`` exists only to keep the distinction explicit and is
  not set from this interface.
"""

from __future__ import annotations

import logging

from app.extensions import db
from app.models.achievement import MemberBadge
from app.models.base import utcnow
from app.services.audit_service import record_audit

log = logging.getLogger(__name__)

MAX_ACTIVE_BADGES = 12
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 600


class BadgeError(Exception):
    pass


def _clean(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:limit]


def award_badge(
    target,
    *,
    name: str,
    description: str | None,
    actor,
    project_id: int | None = None,
    milestone_id: int | None = None,
) -> MemberBadge:
    """Grant a recognition badge. Requires a CEO actor."""
    if actor is None or not getattr(actor, "is_ceo", False):
        raise BadgeError("Only the CEO can award recognition badges.")
    if target is None or getattr(target, "id", None) is None:
        raise BadgeError("Choose a member to recognise.")

    name = _clean(name, MAX_NAME_LENGTH)
    if not name or len(name) < 3:
        raise BadgeError("Give the badge a name of at least 3 characters.")

    description = _clean(description, MAX_DESCRIPTION_LENGTH)

    active_count = MemberBadge.query.filter_by(user_id=target.id, is_active=True).count()
    if active_count >= MAX_ACTIVE_BADGES:
        raise BadgeError(
            f"{target.display_name} already has {MAX_ACTIVE_BADGES} active badges. "
            "Revoke one before adding another."
        )

    duplicate = MemberBadge.query.filter_by(
        user_id=target.id, name=name, is_active=True
    ).first()
    if duplicate is not None:
        raise BadgeError("That member already holds an active badge with this name.")

    badge = MemberBadge(
        user_id=target.id,
        name=name,
        description=description,
        awarded_by_id=actor.id,
        awarded_at=utcnow(),
        project_id=project_id,
        milestone_id=milestone_id,
        is_active=True,
        is_formal_appointment=False,
    )
    profile = _profile_id(target)
    badge.profile_id = profile
    db.session.add(badge)
    db.session.commit()

    record_audit(
        "badge.awarded",
        entity_type="member_badge",
        entity_id=badge.id,
        summary=f"Recognition badge “{name}” awarded to {target.username}",
        after={"name": name, "description": description, "user_id": target.id},
        user_id=actor.id,
    )

    _notify(target, badge, revoked=False)
    return badge


def update_badge(badge: MemberBadge, *, name: str, description: str | None, actor) -> MemberBadge:
    if actor is None or not getattr(actor, "is_ceo", False):
        raise BadgeError("Only the CEO can edit recognition badges.")
    name = _clean(name, MAX_NAME_LENGTH)
    if not name or len(name) < 3:
        raise BadgeError("Give the badge a name of at least 3 characters.")
    description = _clean(description, MAX_DESCRIPTION_LENGTH)

    before = {"name": badge.name, "description": badge.description}
    badge.name = name
    badge.description = description
    db.session.commit()

    record_audit(
        "badge.updated",
        entity_type="member_badge",
        entity_id=badge.id,
        summary=f"Recognition badge for {badge.user_id} updated",
        before=before,
        after={"name": name, "description": description},
        user_id=actor.id,
    )
    return badge


def revoke_badge(badge: MemberBadge, *, reason: str, actor) -> MemberBadge:
    """Revoke recognition without deleting the historical record."""
    if actor is None or not getattr(actor, "is_ceo", False):
        raise BadgeError("Only the CEO can revoke recognition badges.")
    reason = _clean(reason, MAX_DESCRIPTION_LENGTH)
    if not reason or len(reason) < 10:
        raise BadgeError("Record a reason of at least 10 characters for the revocation.")

    badge.is_active = False
    badge.revoked_at = utcnow()
    badge.revoked_by_id = actor.id
    badge.revoke_reason = reason
    db.session.commit()

    record_audit(
        "badge.revoked",
        entity_type="member_badge",
        entity_id=badge.id,
        summary=f"Recognition badge “{badge.name}” revoked for {badge.user_id}",
        before={"is_active": True},
        after={"is_active": False, "reason": reason},
        user_id=actor.id,
    )

    _notify(badge.user, badge, revoked=True)
    return badge


def reactivate_badge(badge: MemberBadge, *, actor) -> MemberBadge:
    if actor is None or not getattr(actor, "is_ceo", False):
        raise BadgeError("Only the CEO can reinstate recognition badges.")
    badge.is_active = True
    badge.revoked_at = None
    badge.revoked_by_id = None
    badge.revoke_reason = None
    db.session.commit()

    record_audit(
        "badge.reactivated",
        entity_type="member_badge",
        entity_id=badge.id,
        summary=f"Recognition badge “{badge.name}” reinstated for {badge.user_id}",
        user_id=actor.id,
    )
    return badge


def active_for(user) -> list[MemberBadge]:
    if user is None or getattr(user, "id", None) is None:
        return []
    return (
        MemberBadge.query.filter_by(user_id=user.id, is_active=True)
        .order_by(MemberBadge.awarded_at.desc())
        .all()
    )


def all_for(user, *, include_revoked: bool = True) -> list[MemberBadge]:
    query = MemberBadge.query.filter_by(user_id=user.id)
    if not include_revoked:
        query = query.filter_by(is_active=True)
    return query.order_by(MemberBadge.awarded_at.desc()).all()


def _profile_id(user) -> int | None:
    from app.services.xp_service import ensure_profile

    try:
        return ensure_profile(user).id
    except Exception:  # pragma: no cover
        return None


def _notify(target, badge: MemberBadge, *, revoked: bool) -> None:
    try:
        from app.services.notification_service import notify

        notify(
            target,
            "BADGE_REVOKED" if revoked else "BADGE_AWARDED",
            title=(
                f"Recognition revoked: {badge.name}"
                if revoked
                else f"Recognition awarded: {badge.name}"
            ),
            body=badge.revoke_reason if revoked else badge.description,
            link_endpoint="profile.detail",
            link_params=f"username={target.username}",
            subject_type="member_badge",
            subject_id=badge.id,
            dedupe_key=f"badge:{badge.id}:{'revoked' if revoked else 'awarded'}",
            actor=badge.awarded_by if not revoked else badge.revoked_by,
        )
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Badge notification failed for %s", target.id)
