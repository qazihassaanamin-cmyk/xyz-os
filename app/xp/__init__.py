"""Contribution and rewards.

Members see their own level, progress, achievements, challenges and a complete
explanation of every award. The leaderboard is opt-in and deliberately not a
single ranking: categories are separated so a member with a harder brief or
fewer hours is not compared unfairly against raw output.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models.user import User
from app.models.xp import XpReason, XpTransaction
from app.permissions import can_manage_xp_rules, can_view_xp_history, is_ceo, login_required
from app.services import (
    achievement_service,
    activity_service,
    challenge_service,
    level_service,
    settings_service,
    streak_service,
    xp_service,
)
from app.utils.dates import company_today
from app.utils.pagination import paginate

bp = Blueprint("xp", __name__)


def _category_boards(*, start, end, limit: int = 8) -> dict:
    """Separate contribution categories instead of one blended score.

    Each board answers a different question: who delivered, who delivered
    cleanly, who kept the daily discipline, and who unblocked others.
    """
    from app.models.focus import FocusSession, FocusStatus
    from app.models.task import Review, Submission, SubmissionStatus

    members = User.query.filter_by(is_active_flag=True).all()

    delivery, quality, consistency, collaboration = [], []

    for member in members:
        approved = Submission.query.filter(
            Submission.submitted_by_id == member.id,
            Submission.status == SubmissionStatus.APPROVED.value,
            Submission.updated_at >= start,
            Submission.updated_at <= end,
        ).count()
        delivery.append({"user": member, "value": approved})

        reviews_given = Review.query.filter(
            Review.reviewer_id == member.id,
            Review.decided_at >= start,
            Review.decided_at <= end,
        ).count()
        first_pass = Submission.query.filter(
            Submission.submitted_by_id == member.id,
            Submission.status == SubmissionStatus.APPROVED.value,
            Submission.revision_number == 1,
            Submission.updated_at >= start,
            Submission.updated_at <= end,
        ).count()
        quality.append(
            {
                "user": member,
                "value": first_pass,
                "detail": f"{first_pass} accepted first time",
                "reviews": reviews_given,
            }
        )

        focus_days = FocusSession.query.filter(
            FocusSession.user_id == member.id,
            FocusSession.status == FocusStatus.COMPLETED.value,
            FocusSession.credited.is_(True),
            FocusSession.workday_date >= start.date(),
            FocusSession.workday_date <= end.date(),
        ).count()
        consistency.append({"user": member, "value": focus_days, "detail": "credited focus days"})

        helped = XpTransaction.query.filter(
            XpTransaction.user_id == member.id,
            XpTransaction.reason.in_(
                [XpReason.BLOCKER_RESOLVED.value, XpReason.BLOCKER_HELPED.value]
            ),
            XpTransaction.created_at >= start,
            XpTransaction.created_at <= end,
            XpTransaction.amount > 0,
        ).count()
        collaboration.append({"user": member, "value": helped, "detail": "blockers resolved or supported"})

    for board in (delivery, quality, consistency, collaboration):
        board.sort(key=lambda row: row["value"], reverse=True)

    return {
        "delivery": delivery[:limit],
        "quality": quality[:limit],
        "consistency": consistency[:limit],
        "collaboration": collaboration[:limit],
    }


@bp.route("/")
@login_required
def index():
    """The member's own contribution page."""
    profile = xp_service.ensure_profile(current_user)
    ladder = level_service.progress_for_lifetime_xp(profile.lifetime_xp or 0)

    recent = request.args.get("filter", "all")
    query = XpTransaction.query.filter_by(user_id=current_user.id)
    if recent == "awards":
        query = query.filter(XpTransaction.amount > 0)
    elif recent == "adjustments":
        query = query.filter(XpTransaction.amount < 0)
    elif recent == "tasks":
        query = query.filter(XpTransaction.source_type == "submission")

    pagination = paginate(
        query.order_by(XpTransaction.created_at.desc(), XpTransaction.id.desc()),
        per_page=25,
    )

    from app.services.workday_service import week_bounds

    week_start, week_end = week_bounds(company_today())
    from app.utils.dates import day_bounds_utc

    month_start = week_start - timedelta(days=30)
    m_start, _ = day_bounds_utc(month_start)
    _, m_end = day_bounds_utc(company_today())

    return render_template(
        "xp/index.html",
        page_title="Contribution",
        profile=profile,
        ladder=ladder,
        pagination=pagination,
        transactions=pagination.items,
        filter_name=recent,
        achievements=achievement_service.for_user(current_user),
        challenges=challenge_service.summary_for(current_user),
        weekly=activity_service.weekly_progress(current_user),
        quality=activity_service.quality_indicators(current_user),
        focus_stats=_focus_snapshot(current_user),
        month_xp=xp_service.total_awarded_between(current_user, m_start, m_end),
        next_milestone=streak_service.next_milestone(profile.focus_streak or 0),
        gamification_on=settings_service.get_bool("gamification_enabled", True),
    )


def _focus_snapshot(user) -> dict:
    from app.services.focus_service import stats_for

    return stats_for(user, days=30)


@bp.route("/leaderboard")
@login_required
def leaderboard():
    """Recognition page. Enabled or disabled by the CEO from settings."""
    if not settings_service.get_bool("leaderboard_enabled", True):
        return render_template(
            "xp/leaderboard_disabled.html",
            page_title="Contribution board",
        )

    show_totals = settings_service.get_bool("leaderboard_show_totals", True)
    rows = xp_service.leaderboard()

    period = request.args.get("period", "lifetime")
    from app.utils.dates import day_bounds_utc
    from app.services.workday_service import week_bounds

    today = company_today()
    if period == "week":
        start_date, end_date = week_bounds(today)
    elif period == "month":
        start_date = today.replace(day=1)
        end_date = today
    else:
        start_date, end_date = today - timedelta(days=3650), today

    start_dt, _ = day_bounds_utc(start_date)
    _, end_dt = day_bounds_utc(end_date)

    period_rows = []
    for profile, user in rows:
        earned = xp_service.total_awarded_between(user, start_dt, end_dt)
        period_rows.append({"profile": profile, "user": user, "earned": earned})
    if period != "lifetime":
        period_rows.sort(key=lambda row: row["earned"], reverse=True)

    return render_template(
        "xp/leaderboard.html",
        page_title="Contribution board",
        rows=period_rows,
        period=period,
        show_totals=show_totals,
        recent_achievements=achievement_service.recent(limit=10),
        categories=_category_boards(start=start_dt, end=end_dt),
        start=start_date,
        end=end_date,
        challenges=challenge_service.summary_for(current_user),
    )


# ---------------------------------------------------------------------------
# CEO: rules, levels, adjustments and review of unusual activity
# ---------------------------------------------------------------------------

@bp.route("/admin")
@login_required
def admin():
    """XP administration. The system works without ever visiting this page."""
    if not can_manage_xp_rules(current_user):
        abort(403)

    from app.models.xp import LevelDefinition, XpRule

    rules = XpRule.query.order_by(XpRule.id.asc()).all()
    levels = LevelDefinition.query.order_by(LevelDefinition.level.asc()).all()
    members = User.query.filter_by(is_active_flag=True).order_by(User.full_name).all()

    recent_adjustments = (
        XpTransaction.query.filter(
            XpTransaction.reason.in_(
                [
                    XpReason.MANUAL_AWARD.value,
                    XpReason.MANUAL_DEDUCTION.value,
                    XpReason.REVERSAL.value,
                    XpReason.ADMIN_ADJUSTMENT.value,
                ]
            )
        )
        .order_by(XpTransaction.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template(
        "xp/admin.html",
        page_title="Rewards configuration",
        rules=rules,
        levels=levels,
        members=members,
        recent_adjustments=recent_adjustments,
        anomalies=_anomalies(),
        difficulty_multipliers=xp_service.DIFFICULTY_MULTIPLIERS,
        reasons=XpReason.values(),
    )


def _anomalies() -> list[dict]:
    """Flag patterns worth a human look. Never an accusation, only a signal."""
    findings: list[dict] = []
    today = company_today()
    window_start = today - timedelta(days=7)

    # Many awards in one day for the same reason is the main farming signal.
    rows = (
        db.session.query(
            XpTransaction.user_id,
            XpTransaction.reason,
            db.func.count(XpTransaction.id).label("count"),
            db.func.sum(XpTransaction.amount).label("total"),
        )
        .filter(
            XpTransaction.amount > 0,
            db.func.date(XpTransaction.created_at) >= window_start,
        )
        .group_by(XpTransaction.user_id, XpTransaction.reason)
        .having(db.func.count(XpTransaction.id) > 12)
        .all()
    )
    for user_id, reason, count, total in rows:
        user = db.session.get(User, user_id)
        findings.append(
            {
                "kind": "volume",
                "user": user,
                "reason": reason,
                "detail": f"{count} awards of this type in the last 7 days ({total} XP).",
            }
        )

    # Reversals concentrated on one member may indicate a rule problem.
    reversals = (
        XpTransaction.query.filter(
            XpTransaction.reason == XpReason.REVERSAL.value,
            XpTransaction.created_at >= db.func.datetime(window_start),
        )
        .order_by(XpTransaction.created_at.desc())
        .limit(10)
        .all()
    )
    for row in reversals:
        findings.append(
            {
                "kind": "reversal",
                "user": row.user,
                "reason": row.reason,
                "detail": row.note or "An earlier award was reversed.",
            }
        )

    # Members whose balance disagrees with their ledger.
    for profile, user in xp_service.leaderboard():
        if xp_service.ledger_balance(user) != (profile.balance or 0):
            findings.append(
                {
                    "kind": "drift",
                    "user": user,
                    "reason": "balance",
                    "detail": (
                        f"Cached balance {profile.balance} does not match the ledger "
                        f"({xp_service.ledger_balance(user)}). Run `flask maintenance`."
                    ),
                }
            )

    return findings[:20]


@bp.route("/admin/rules/<int:rule_id>", methods=["POST"])
@login_required
def update_rule(rule_id: int):
    """Change a rule's value, cap or enabled state."""
    if not can_manage_xp_rules(current_user):
        abort(403)

    from app.models.xp import XpRule
    from app.services.audit_service import record_audit

    rule = db.session.get(XpRule, rule_id)
    if rule is None:
        abort(404)

    before = {"points": rule.points, "is_enabled": rule.is_enabled, "daily_cap": rule.daily_cap}

    points = request.form.get("points", type=int)
    if points is not None:
        if not 0 <= points <= 5000:
            flash("A rule value must be between 0 and 5000.", "error")
            return redirect(url_for("xp.admin"))
        rule.points = points

    cap = request.form.get("daily_cap")
    if cap is not None:
        cap = cap.strip()
        if cap == "":
            rule.daily_cap = None
        else:
            try:
                value = int(cap)
            except ValueError:
                flash("The daily cap must be a whole number or blank.", "error")
                return redirect(url_for("xp.admin"))
            rule.daily_cap = max(0, value)

    if request.form.get("is_enabled") is not None:
        rule.is_enabled = request.form.get("is_enabled") == "1"

    label = (request.form.get("label") or "").strip()
    if label:
        rule.label = label[:120]

    db.session.commit()

    record_audit(
        "xp.rule_updated",
        entity_type="xp_rule",
        entity_id=rule.id,
        summary=f"Reward rule “{rule.label}” updated",
        before=before,
        after={"points": rule.points, "is_enabled": rule.is_enabled, "daily_cap": rule.daily_cap},
        user_id=current_user.id,
    )
    flash(f"Rule “{rule.label}” updated. Existing awards are unchanged.", "success")
    return redirect(url_for("xp.admin") + "#rules")


@bp.route("/admin/levels/<int:level_id>", methods=["POST"])
@login_required
def update_level(level_id: int):
    if not can_manage_xp_rules(current_user):
        abort(403)

    from app.models.xp import LevelDefinition
    from app.services.audit_service import record_audit

    level = db.session.get(LevelDefinition, level_id)
    if level is None:
        abort(404)

    before = {"name": level.name, "min_lifetime_xp": level.min_lifetime_xp}
    name = (request.form.get("name") or "").strip()
    if name:
        level.name = name[:40]
    threshold = request.form.get("min_lifetime_xp", type=int)
    if threshold is not None and threshold >= 0:
        level.min_lifetime_xp = threshold

    db.session.commit()

    # Every member's cached level is recomputed from the new thresholds.
    for member in User.query.filter_by(is_active_flag=True).all():
        xp_service.recompute_profile(member)

    record_audit(
        "xp.level_updated",
        entity_type="level_definition",
        entity_id=level.id,
        summary=f"Level {level.level} updated",
        before=before,
        after={"name": level.name, "min_lifetime_xp": level.min_lifetime_xp},
        user_id=current_user.id,
    )
    flash("Level updated and every member's progress recalculated.", "success")
    return redirect(url_for("xp.admin") + "#levels")


@bp.route("/admin/adjust", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def adjust():
    """Manual award or deduction. Always requires a written reason."""
    if not can_manage_xp_rules(current_user):
        abort(403)

    target = db.session.get(User, request.form.get("user_id", type=int))
    if target is None:
        flash("Choose a member.", "error")
        return redirect(url_for("xp.admin"))

    amount = request.form.get("amount", type=int)
    note = request.form.get("note", "")

    if amount is None:
        flash("Enter an XP amount.", "error")
        return redirect(url_for("xp.admin"))
    if amount < 0 and not settings_service.get_bool("xp_deductions_enabled", True):
        flash("Manual deductions are switched off in settings.", "error")
        return redirect(url_for("xp.admin"))

    try:
        xp_service.manual_adjustment(
            target, amount=amount, note=note, actor=current_user
        )
    except xp_service.XpError as exc:
        flash(str(exc), "error")
    else:
        flash(f"{amount:+d} XP applied to {target.display_name}.", "success")
    return redirect(url_for("xp.admin") + "#adjustments")


@bp.route("/admin/reverse/<int:transaction_id>", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def reverse(transaction_id: int):
    """Reverse an incorrect award. The original row is never deleted."""
    if not can_manage_xp_rules(current_user):
        abort(403)

    transaction = db.session.get(XpTransaction, transaction_id)
    if transaction is None:
        abort(404)

    try:
        xp_service.reverse_transaction(
            transaction, note=request.form.get("note", ""), actor=current_user
        )
    except xp_service.XpError as exc:
        flash(str(exc), "error")
    else:
        flash(
            "The award was reversed with a compensating entry. History is intact.",
            "success",
        )
    return redirect(request.referrer or url_for("xp.admin"))


@bp.route("/admin/recompute", methods=["POST"])
@login_required
def recompute():
    """Rebuild every cached balance from the ledger."""
    if not can_manage_xp_rules(current_user):
        abort(403)

    for member in User.query.filter_by(is_active_flag=True).all():
        xp_service.recompute_profile(member)

    flash("Balances and levels rebuilt from the ledger.", "success")
    return redirect(url_for("xp.admin"))


@bp.route("/admin/challenges", methods=["POST"])
@login_required
def create_challenge():
    """Add a challenge for the current week."""
    if not can_manage_xp_rules(current_user):
        abort(403)

    from app.models.challenge import Challenge, ChallengeMetric
    from app.services.audit_service import record_audit
    from app.services.workday_service import week_bounds

    title = (request.form.get("title") or "").strip()
    metric = (request.form.get("metric") or "").strip().upper()
    target_value = request.form.get("target_value", type=int) or 1
    reward = request.form.get("reward_xp", type=int) or 0

    if len(title) < 4:
        flash("Give the challenge a clear title.", "error")
        return redirect(url_for("xp.admin") + "#challenges")
    if metric not in ChallengeMetric.values():
        flash("Choose a valid metric.", "error")
        return redirect(url_for("xp.admin") + "#challenges")

    start, end = week_bounds(company_today())
    challenge = Challenge(
        title=title[:160],
        description=(request.form.get("description") or "").strip()[:1000] or None,
        metric=metric,
        target_value=max(1, min(target_value, 100)),
        reward_xp=max(0, min(reward, 500)),
        week_start=start,
        week_end=end,
        created_by_id=current_user.id,
        is_active=True,
    )
    db.session.add(challenge)
    db.session.commit()

    record_audit(
        "challenge.created",
        entity_type="challenge",
        entity_id=challenge.id,
        summary=f"Challenge “{challenge.title}” created for the current week",
        user_id=current_user.id,
    )
    flash("Challenge created for this week.", "success")
    return redirect(url_for("xp.admin") + "#challenges")


@bp.route("/admin/challenges/<int:challenge_id>/toggle", methods=["POST"])
@login_required
def toggle_challenge(challenge_id: int):
    if not can_manage_xp_rules(current_user):
        abort(403)

    from app.models.challenge import Challenge

    challenge = db.session.get(Challenge, challenge_id)
    if challenge is None:
        abort(404)

    challenge.is_active = not challenge.is_active
    db.session.commit()
    flash(
        f"Challenge “{challenge.title}” is now "
        f"{'active' if challenge.is_active else 'inactive'}.",
        "success",
    )
    return redirect(url_for("xp.admin") + "#challenges")


# ---------------------------------------------------------------------------
# Reading another member's ledger
# ---------------------------------------------------------------------------

@bp.route("/history/<username>")
@login_required
def member_history(username: str):
    """A member's full XP history. Members see their own; the CEO sees all."""
    user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
    if user is None or not user.is_active:
        abort(404)
    if not can_view_xp_history(current_user, user):
        abort(403)

    pagination = paginate(
        XpTransaction.query.filter_by(user_id=user.id).order_by(
            XpTransaction.created_at.desc(), XpTransaction.id.desc()
        ),
        per_page=40,
    )
    return render_template(
        "xp/member_history.html",
        page_title=f"Contribution history — {user.display_name}",
        member=user,
        profile=xp_service.ensure_profile(user),
        pagination=pagination,
        transactions=pagination.items,
        is_ceo=is_ceo(current_user),
    )
