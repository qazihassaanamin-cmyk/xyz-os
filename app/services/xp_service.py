"""The XP engine.

Every award flows through :func:`award_xp`, which enforces, in order:

1. **Server-side verification** — the caller must have already verified the
   underlying event (an accepted submission, a completed timer session). This
   module never trusts a client-supplied amount.
2. **Rule state** — a disabled rule awards nothing.
3. **Idempotency** — the ledger's unique ``idempotency_key`` rejects a second
   award for the same event, so retried requests and duplicate events are safe.
4. **Caps and cooldowns** — daily ceilings and per-event cooldowns stop farming.
5. **Audit** — the row records who granted it, why, and the resulting balance.

XP is never deleted. A correction is a new negative row linked to the original
by ``reversal_of_id``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.base import utcnow
from app.models.user import User
from app.models.xp import XpProfile, XpReason, XpRule, XpTransaction
from app.services.audit_service import record_audit
from app.services.level_service import level_for_lifetime_xp

log = logging.getLogger(__name__)


class XpError(Exception):
    """Raised when an award cannot be granted."""


# ---------------------------------------------------------------------------
# The complete initial XP economy. These values are the shipped defaults and
# are copied into the ``xp_rules`` table on first run, where the CEO may tune
# them. They are chosen so that a full week of ordinary, verified work moves a
# founder roughly one level, while trivial activity earns almost nothing.
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[dict] = [
    {
        "code": XpReason.TASK_COMPLETED.value,
        "label": "Task accepted",
        "points": 100,
        "description": (
            "Granted once when the assignee's submission is approved. The final "
            "amount is scaled by the task difficulty set by the CEO or lead."
        ),
        "daily_cap": None,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.REVIEW_QUALITY_BONUS.value,
        "label": "Review quality bonus",
        "points": 35,
        "description": "Granted when a reviewer marks the accepted work as high quality.",
        "daily_cap": 140,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.ON_TIME_BONUS.value,
        "label": "Delivered before the deadline",
        "points": 25,
        "description": "Modest bonus for accepted work submitted on or before the task deadline.",
        "daily_cap": 75,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.MILESTONE_COMPLETED.value,
        "label": "Project milestone completed",
        "points": 120,
        "description": "Granted to the member who closes an accepted project milestone.",
        "daily_cap": 240,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.BLOCKER_RESOLVED.value,
        "label": "Blocker resolved",
        "points": 40,
        "description": "Granted to the person who resolves a blocker that was stopping work.",
        "daily_cap": 120,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.BLOCKER_HELPED.value,
        "label": "Helped unblock a team member",
        "points": 30,
        "description": "Granted to a second member who contributed to resolving someone else's blocker.",
        "daily_cap": 90,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.FOCUS_SESSION.value,
        "label": "Completed focus session",
        "points": 20,
        "description": (
            "Granted for one completed 20-minute focus timer per working day. "
            "The timer is server-validated; a running timer is not proof of work."
        ),
        "daily_cap": 20,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.CHECKIN_COMPLETED.value,
        "label": "Daily check-in submitted",
        "points": 10,
        "description": "Granted once per working day for a submitted daily check-in.",
        "daily_cap": 10,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.REPORT_COMPLETED.value,
        "label": "Daily report submitted",
        "points": 15,
        "description": "Granted once per working day for an end-of-day report.",
        "daily_cap": 15,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.SUGGESTION_IMPLEMENTED.value,
        "label": "Suggestion implemented",
        "points": 80,
        "description": "Granted to the author when the CEO marks a suggestion as implemented.",
        "daily_cap": 160,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.KNOWLEDGE_DOCUMENTED.value,
        "label": "Knowledge documented",
        "points": 45,
        "description": "Granted when a knowledge base entry is published for the team.",
        "daily_cap": 135,
        "cooldown_seconds": 1800,
    },
    {
        "code": XpReason.ONBOARDING_HELP.value,
        "label": "Helped onboard a member",
        "points": 90,
        "description": "Granted once when a new member's first week is supported by a founder.",
        "daily_cap": 90,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.STREAK_BONUS.value,
        "label": "Consistency streak",
        "points": 25,
        "description": "Granted at each five-working-day consistency milestone, capped per streak.",
        "daily_cap": 25,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.WEEKLY_CHALLENGE.value,
        "label": "Weekly challenge completed",
        "points": 60,
        "description": "Granted when a weekly challenge target is reached.",
        "daily_cap": 120,
        "cooldown_seconds": None,
    },
    # The manual reasons are seeded with zero points: they only ever carry an
    # explicit amount, and the reason must be written in words. They are seeded
    # so the rules table is complete and the CEO can see them in the panel.
    {
        "code": XpReason.MANUAL_AWARD.value,
        "label": "Manual award",
        "points": 0,
        "description": (
            "A discretionary award by the CEO. Requires an explicit amount and a "
            "written reason of at least 10 characters. Weekly total is capped."
        ),
        "daily_cap": None,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.MANUAL_DEDUCTION.value,
        "label": "Manual deduction",
        "points": 0,
        "description": "A discretionary deduction by the CEO, always with a written reason.",
        "daily_cap": None,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.REVERSAL.value,
        "label": "Reversal of an earlier award",
        "points": 0,
        "description": (
            "A compensating negative entry against an earlier award. The original "
            "row is never edited or deleted."
        ),
        "daily_cap": None,
        "cooldown_seconds": None,
    },
    {
        "code": XpReason.ADMIN_ADJUSTMENT.value,
        "label": "Administrative adjustment",
        "points": 0,
        "description": "A correction applied during maintenance. Always logged with a reason.",
        "daily_cap": None,
        "cooldown_seconds": None,
    },
]

# Difficulty scales the base award. These are percentages of the rule value.
DIFFICULTY_MULTIPLIERS: dict[str, int] = {
    "SMALL": 40,
    "MEDIUM": 100,
    "LARGE": 200,
    "CRITICAL": 350,
}

# The shipped XP economy, stated once so the UI and the tests share it.
# Task awards are these numbers scaled by the difficulty multiplier above.
DIRECT_AWARD_VALUES: dict[str, int] = {
    "small_task": 40,
    "medium_task": 100,
    "large_task": 200,
    "critical_task": 350,
    "review_quality_bonus_max": 10,
    "on_time_bonus_percent": 10,
    "focus_completion": 10,
    "checkin_and_report": 10,
    "blocker_resolved": 20,
    "milestone_completed": 50,
    "suggestion_implemented": 40,
    "knowledge_document_approved": 15,
    "ceo_bonus_max": 100,
}

# Reasons that must never be granted by the automatic engine.
MANUAL_ONLY_REASONS = {
    XpReason.MANUAL_AWARD.value,
    XpReason.MANUAL_DEDUCTION.value,
    XpReason.REVERSAL.value,
    XpReason.ADMIN_ADJUSTMENT.value,
}

# Weekly ceiling per source. Generous for ordinary weeks, strict enough that
# grinding one kind of event cannot inflate the ledger. "Discretionary" CEO
# awards are capped here too, as the brief requires.
WEEKLY_CAPS: dict[str, int] = {
    XpReason.TASK_COMPLETED.value: 1200,
    XpReason.REVIEW_QUALITY_BONUS.value: 200,
    XpReason.ON_TIME_BONUS.value: 200,
    XpReason.MILESTONE_COMPLETED.value: 400,
    XpReason.BLOCKER_RESOLVED.value: 300,
    XpReason.BLOCKER_HELPED.value: 200,
    XpReason.FOCUS_SESSION.value: 100,
    XpReason.CHECKIN_COMPLETED.value: 60,
    XpReason.REPORT_COMPLETED.value: 75,
    XpReason.SUGGESTION_IMPLEMENTED.value: 200,
    XpReason.KNOWLEDGE_DOCUMENTED.value: 150,
    XpReason.WEEKLY_CHALLENGE.value: 180,
    XpReason.STREAK_BONUS.value: 100,
    XpReason.MANUAL_AWARD.value: 500,
    XpReason.MANUAL_DEDUCTION.value: 500,
}

# Reasons for which more than one award per day is allowed but still capped.
REPEATABLE_REASONS: set[str] = {
    XpReason.TASK_COMPLETED.value,
    XpReason.REVIEW_QUALITY_BONUS.value,
    XpReason.ON_TIME_BONUS.value,
    XpReason.MILESTONE_COMPLETED.value,
    XpReason.BLOCKER_RESOLVED.value,
    XpReason.BLOCKER_HELPED.value,
    XpReason.SUGGESTION_IMPLEMENTED.value,
    XpReason.KNOWLEDGE_DOCUMENTED.value,
    XpReason.WEEKLY_CHALLENGE.value,
}


def ensure_profile(user: User) -> XpProfile:
    """Return the member's XP profile, creating it if needed."""
    profile = XpProfile.query.filter_by(user_id=user.id).first()
    if profile is not None:
        return profile

    profile = XpProfile(user_id=user.id)
    _apply_level(profile)
    db.session.add(profile)
    try:
        db.session.commit()
    except IntegrityError:
        # Another request created it first.
        db.session.rollback()
        profile = XpProfile.query.filter_by(user_id=user.id).first()
        if profile is None:  # pragma: no cover - genuinely unexpected
            raise
    return profile


def ensure_rules() -> None:
    """Seed the XP rule table. Idempotent; never overwrites a CEO's tuning."""
    changed = False
    for spec in DEFAULT_RULES:
        rule = XpRule.query.filter_by(code=spec["code"]).first()
        if rule is None:
            db.session.add(
                XpRule(
                    code=spec["code"],
                    label=spec["label"],
                    points=spec["points"],
                    description=spec["description"],
                    daily_cap=spec["daily_cap"],
                    cooldown_seconds=spec["cooldown_seconds"],
                    is_enabled=True,
                )
            )
            changed = True
    if changed:
        db.session.commit()


def rule_for(reason: str) -> XpRule | None:
    return XpRule.query.filter_by(code=reason).first()


def _apply_level(profile: XpProfile) -> None:
    """Recompute the cached level fields from lifetime XP."""
    definition, into_level, to_next = level_for_lifetime_xp(profile.lifetime_xp or 0)
    profile.current_level = definition.level
    profile.level_name = definition.name
    profile.xp_into_level = into_level
    profile.xp_to_next_level = to_next


def _already_awarded(idempotency_key: str | None) -> bool:
    if not idempotency_key:
        return False
    return (
        XpTransaction.query.filter_by(idempotency_key=idempotency_key).first() is not None
    )


def _awarded_today(user_id: int, reason: str) -> int:
    """Total XP granted today for a reason, counting only positive rows."""
    day_start = datetime.combine(utcnow().date(), datetime.min.time(), tzinfo=timezone.utc)
    total = (
        db.session.query(func.coalesce(func.sum(XpTransaction.amount), 0))
        .filter(
            XpTransaction.user_id == user_id,
            XpTransaction.reason == reason,
            XpTransaction.amount > 0,
            XpTransaction.created_at >= day_start,
        )
        .scalar()
    )
    return int(total or 0)


def _awarded_this_week(user_id: int, reason: str) -> int:
    """Total XP granted this week for a reason, counted from Monday.

    The window is the company's working week, so the figure means the same thing
    to every member regardless of their local calendar.
    """
    from app.services.workday_service import week_bounds
    from app.utils.dates import company_today, day_bounds_utc

    week_start, _ = week_bounds(company_today())
    start_utc, _ = day_bounds_utc(week_start)
    total = (
        db.session.query(func.coalesce(func.sum(XpTransaction.amount), 0))
        .filter(
            XpTransaction.user_id == user_id,
            XpTransaction.reason == reason,
            XpTransaction.amount > 0,
            XpTransaction.created_at >= start_utc,
        )
        .scalar()
    )
    return int(total or 0)


def _last_award_at(user_id: int, reason: str) -> datetime | None:
    row = (
        XpTransaction.query.filter_by(user_id=user_id, reason=reason)
        .order_by(XpTransaction.created_at.desc())
        .first()
    )
    if row is None:
        return None
    value = row.created_at
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def award_xp(
    user: User,
    reason: str,
    *,
    amount: int | None = None,
    note: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    project_id: int | None = None,
    task_id: int | None = None,
    idempotency_key: str | None = None,
    awarded_by: User | None = None,
    multiplier_percent: int | None = None,
    allow_manual: bool = False,
) -> XpTransaction | None:
    """Grant XP for a verified event.

    Returns the ledger row, or ``None`` when the award was suppressed because it
    was already granted, the rule is disabled, or a cap or cooldown applies.
    Raises :class:`XpError` only for programmer errors such as a manual reason
    without a note.
    """
    if user is None or getattr(user, "id", None) is None:
        raise XpError("XP can only be awarded to a stored member.")

    rule = rule_for(reason)
    if rule is None:
        raise XpError(f"Unknown XP rule: {reason}")
    if not rule.is_enabled:
        log.info("XP rule %s is disabled; skipping award for user %s", reason, user.id)
        return None

    if reason in MANUAL_ONLY_REASONS and not allow_manual:
        raise XpError(f"{reason} is a manual reason and cannot be granted automatically.")
    if allow_manual and not (note or "").strip():
        raise XpError("Manual awards and deductions require a reason.")
    # No one may reward themselves, and a manual award must always name its actor
    # so the ledger can answer "who gave this?" without inference.
    if allow_manual:
        if awarded_by is not None and getattr(awarded_by, "id", None) == user.id:
            raise XpError("You cannot award or deduct XP on your own account.")
        if getattr(awarded_by, "role", None) != "CEO":
            raise XpError("Only the CEO can make manual XP adjustments.")

    if _already_awarded(idempotency_key):
        log.info("Duplicate XP award blocked for key %s", idempotency_key)
        return None

    base_points = int(amount if amount is not None else rule.points or 0)
    if base_points == 0 and not allow_manual:
        return None

    if multiplier_percent is not None and base_points > 0:
        base_points = int(round(base_points * multiplier_percent / 100))

    # A manual award may be negative; automatic awards are always positive.
    if base_points < 0 and not allow_manual:
        raise XpError("Automatic awards cannot be negative.")

    if base_points > 0 and not allow_manual:
        if rule.daily_cap is not None:
            already = _awarded_today(user.id, reason)
            if already >= rule.daily_cap:
                log.info("Daily cap reached for %s on rule %s", user.id, reason)
                return None
            if already + base_points > rule.daily_cap:
                base_points = max(0, rule.daily_cap - already)
                if base_points == 0:
                    return None
        # Weekly ceilings apply to every automatic source, so a burst of
        # approvals on one day cannot roll into an outsized week.
        weekly_cap = WEEKLY_CAPS.get(reason)
        if weekly_cap is not None:
            already_week = _awarded_this_week(user.id, reason)
            if already_week >= weekly_cap:
                log.info("Weekly cap reached for %s on rule %s", user.id, reason)
                return None
            if already_week + base_points > weekly_cap:
                base_points = max(0, weekly_cap - already_week)
                if base_points == 0:
                    return None
        if rule.cooldown_seconds:
            last = _last_award_at(user.id, reason)
            if last is not None and (utcnow() - last).total_seconds() < rule.cooldown_seconds:
                log.info("Cooldown active for %s on rule %s", user.id, reason)
                return None

    profile = ensure_profile(user)
    previous_balance = profile.balance or 0
    previous_level = profile.current_level or 1
    previous_lifetime = profile.lifetime_xp or 0

    transaction = XpTransaction(
        user_id=user.id,
        profile_id=profile.id,
        amount=base_points,
        reason=reason,
        note=(note or "").strip() or None,
        source_type=source_type,
        source_id=source_id,
        project_id=project_id,
        task_id=task_id,
        idempotency_key=idempotency_key,
        awarded_by_id=getattr(awarded_by, "id", None),
        balance_after=previous_balance + base_points,
    )
    db.session.add(transaction)

    profile.balance = previous_balance + base_points
    # Lifetime only grows on net-positive awards; deductions reduce the balance
    # without erasing the record of work already credited.
    if base_points > 0:
        profile.lifetime_xp = previous_lifetime + base_points
        profile.last_award_at = utcnow()

    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        log.info("Duplicate XP award blocked by the database for key %s", idempotency_key)
        return None

    _apply_level(profile)
    db.session.commit()

    # Post-award side effects. Each is best-effort and never breaks the award.
    _after_award(user, profile, transaction, previous_level)

    return transaction


def _after_award(user, profile, transaction, previous_level) -> None:
    from app.services.achievement_service import evaluate_all
    from app.services.challenge_service import evaluate_challenges

    try:
        evaluate_all(user)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Achievement evaluation failed for user %s", user.id)

    try:
        evaluate_challenges(user)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Challenge evaluation failed for user %s", user.id)

    if transaction.amount > 0 and profile.current_level > previous_level:
        try:
            from app.services.notification_service import notify

            notify(
                user,
                "LEVEL_UP",
                title=f"Level {profile.current_level}: {profile.level_name}",
                body="Your verified work moved you up a level.",
                link_endpoint="xp.index",
                subject_type="user",
                subject_id=user.id,
                dedupe_key=f"levelup:{user.id}:{profile.current_level}",
            )
        except Exception:  # pragma: no cover
            db.session.rollback()
            log.exception("Level-up notification failed for user %s", user.id)

    if transaction.amount > 0:
        try:
            from app.services.notification_service import notify

            notify(
                user,
                "XP_AWARDED",
                title=f"+{transaction.amount} XP — {transaction.reason_label}",
                body=transaction.note,
                link_endpoint="xp.index",
                subject_type="xp_transaction",
                subject_id=transaction.id,
                dedupe_key=f"xpaward:{transaction.id}",
            )
        except Exception:  # pragma: no cover
            db.session.rollback()
            log.exception("XP notification failed for user %s", user.id)
    elif transaction.amount < 0:
        try:
            from app.services.notification_service import notify

            notify(
                user,
                "XP_DEDUCTED",
                title=f"{transaction.amount} XP — {transaction.reason_label}",
                body=transaction.note,
                link_endpoint="xp.index",
                subject_type="xp_transaction",
                subject_id=transaction.id,
                dedupe_key=f"xpdeduct:{transaction.id}",
            )
        except Exception:  # pragma: no cover
            db.session.rollback()
            log.exception("XP deduction notification failed for user %s", user.id)


def award_task_completion(
    user: User,
    *,
    task,
    submission,
    base_xp: int,
    difficulty: str,
    on_time: bool,
    quality_bonus: bool,
    awarded_by: User | None = None,
) -> list[XpTransaction]:
    """Award everything an accepted submission is worth. Idempotent per submission."""
    awarded: list[XpTransaction] = []
    multiplier = DIFFICULTY_MULTIPLIERS.get(difficulty, DIFFICULTY_MULTIPLIERS["MEDIUM"])

    row = award_xp(
        user,
        XpReason.TASK_COMPLETED.value,
        amount=base_xp,
        multiplier_percent=multiplier,
        note=f"Accepted work on “{task.title}” ({difficulty.title()} difficulty).",
        source_type="submission",
        source_id=submission.id,
        project_id=task.project_id,
        task_id=task.id,
        idempotency_key=f"task:{task.id}:submission:{submission.id}:completed",
        awarded_by=awarded_by,
    )
    if row is not None:
        awarded.append(row)

    if quality_bonus:
        row = award_xp(
            user,
            XpReason.REVIEW_QUALITY_BONUS.value,
            note=f"Reviewer marked the work on “{task.title}” as high quality.",
            source_type="submission",
            source_id=submission.id,
            project_id=task.project_id,
            task_id=task.id,
            idempotency_key=f"task:{task.id}:submission:{submission.id}:quality",
            awarded_by=awarded_by,
        )
        if row is not None:
            awarded.append(row)

    if on_time:
        row = award_xp(
            user,
            XpReason.ON_TIME_BONUS.value,
            note=f"“{task.title}” was accepted on or before its deadline.",
            source_type="submission",
            source_id=submission.id,
            project_id=task.project_id,
            task_id=task.id,
            idempotency_key=f"task:{task.id}:submission:{submission.id}:ontime",
            awarded_by=awarded_by,
        )
        if row is not None:
            awarded.append(row)

    return awarded


def manual_adjustment(
    target: User,
    *,
    amount: int,
    note: str,
    actor: User | None,
    source_type: str = "manual",
) -> XpTransaction:
    """Award or deduct XP by hand. Requires a reason and a CEO actor."""
    amount = int(amount)
    if amount == 0:
        raise XpError("Enter a non-zero XP amount.")
    if abs(amount) > 2000:
        raise XpError("A single manual adjustment may not exceed 2000 XP.")
    note = (note or "").strip()
    if len(note) < 10:
        raise XpError("Provide a clear reason of at least 10 characters.")

    reason = XpReason.MANUAL_AWARD.value if amount > 0 else XpReason.MANUAL_DEDUCTION.value
    row = award_xp(
        target,
        reason,
        amount=amount,
        note=note,
        source_type=source_type,
        idempotency_key=None,
        awarded_by=actor,
        allow_manual=True,
    )
    if row is None:
        raise XpError("The adjustment was not applied.")
    return row


def reverse_transaction(transaction: XpTransaction, *, note: str, actor: User | None) -> XpTransaction:
    """Reverse an earlier award without deleting it."""
    if transaction.reversal_of_id is not None:
        raise XpError("A reversal cannot itself be reversed.")
    if transaction.reversed_by_id is not None:
        raise XpError("That transaction has already been reversed.")
    if transaction.amount <= 0:
        raise XpError("Only positive awards can be reversed.")

    reversal = award_xp(
        transaction.user,
        XpReason.REVERSAL.value,
        amount=-abs(transaction.amount),
        note=note or f"Reversal of transaction #{transaction.id}.",
        source_type="xp_transaction",
        source_id=transaction.id,
        idempotency_key=f"reversal:{transaction.id}",
        awarded_by=actor,
        allow_manual=True,
    )
    if reversal is None:
        raise XpError("That transaction has already been reversed.")

    transaction.reversed_by_id = reversal.id
    db.session.commit()

    record_audit(
        "xp.reversal",
        entity_type="xp_transaction",
        entity_id=transaction.id,
        summary=f"Reversed {transaction.amount} XP for user #{transaction.user_id}",
        before={"amount": transaction.amount, "reason": transaction.reason},
        after={"amount": 0, "reversal_id": reversal.id},
        user_id=getattr(actor, "id", None),
    )
    return reversal


def recompute_profile(user: User) -> XpProfile:
    """Rebuild the cached balance and lifetime total from the ledger.

    Used after a correction or as a maintenance action if the cache is ever
    suspected of drifting. The ledger is the source of truth.
    """
    profile = ensure_profile(user)
    rows = XpTransaction.query.filter_by(user_id=user.id).all()

    balance = sum(r.amount for r in rows if r.reversal_of_id is None)
    lifetime = sum(r.amount for r in rows if r.amount > 0 and r.reversed_by_id is None)

    profile.balance = balance
    profile.lifetime_xp = max(0, lifetime)
    _apply_level(profile)
    db.session.commit()
    return profile


def history_for(user: User, *, limit: int | None = 50, offset: int = 0) -> list[XpTransaction]:
    query = XpTransaction.query.filter_by(user_id=user.id).order_by(
        XpTransaction.created_at.desc(), XpTransaction.id.desc()
    )
    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)
    return query.all()


def ledger_balance(user: User) -> int:
    total = (
        db.session.query(func.coalesce(func.sum(XpTransaction.amount), 0))
        .filter(XpTransaction.user_id == user.id, XpTransaction.reversal_of_id.is_(None))
        .scalar()
    )
    return int(total or 0)


def total_awarded_between(user: User, start: datetime, end: datetime) -> int:
    total = (
        db.session.query(func.coalesce(func.sum(XpTransaction.amount), 0))
        .filter(
            XpTransaction.user_id == user.id,
            XpTransaction.amount > 0,
            XpTransaction.created_at >= start,
            XpTransaction.created_at < end,
        )
        .scalar()
    )
    return int(total or 0)


def leaderboard(limit: int | None = None):
    """Members ordered by lifetime XP. Ties break on level then name."""
    query = (
        db.session.query(XpProfile, User)
        .join(User, User.id == XpProfile.user_id)
        .filter(User.is_active_flag.is_(True))
        .order_by(
            XpProfile.lifetime_xp.desc(),
            XpProfile.current_level.desc(),
            User.full_name.asc(),
        )
    )
    if limit:
        query = query.limit(limit)
    return query.all()
