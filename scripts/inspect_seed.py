"""Inspect the seeded database: founders, projects, XP config and achievements."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.achievement import Achievement  # noqa: E402
from app.models.project import Project, ProjectMember  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.xp import LevelDefinition, Setting, XpProfile, XpRule  # noqa: E402

app = create_app("development")

with app.app_context():
    print("=" * 70)
    print("MEMBERS")
    print("=" * 70)
    for user in User.query.order_by(User.id).all():
        profile = XpProfile.query.filter_by(user_id=user.id).first()
        print(
            f"  {user.id}  {user.email:<22} {user.username:<9} {user.role:<7} "
            f"active={user.is_active_flag} title={(user.title or '-')[:38]}"
        )
        if profile:
            print(
                f"        level={profile.current_level} ({profile.level_name}) "
                f"lifetime_xp={profile.lifetime_xp} balance={profile.balance}"
            )

    print()
    print("=" * 70)
    print("PROJECTS")
    print("=" * 70)
    for project in Project.query.order_by(Project.id).all():
        leads = [
            m.user.username for m in project.members if m.role_in_project == "LEAD"
        ]
        print(
            f"  {project.id}  {project.name:<16} {project.slug:<16} "
            f"status={project.status:<10} lead={leads} members={len(project.members)}"
        )

    print()
    print("=" * 70)
    print("CONFIGURATION")
    print("=" * 70)
    print(f"  settings      : {Setting.query.count()}")
    print(f"  xp rules      : {XpRule.query.count()}")
    print(f"  levels        : {LevelDefinition.query.count()}")
    print(f"  achievements  : {Achievement.query.count()}")
    print(f"  xp profiles   : {XpProfile.query.count()}")
    print(f"  memberships   : {ProjectMember.query.count()}")

    print()
    print("XP RULES (the shipped economy)")
    for rule in XpRule.query.order_by(XpRule.id).all():
        cap = rule.daily_cap if rule.daily_cap is not None else "-"
        print(f"  {rule.code:<28} {rule.points:>4}  cap={cap}  enabled={rule.is_enabled}")

    print()
    print("LEVEL LADDER")
    for level in LevelDefinition.query.order_by(LevelDefinition.level).all():
        print(f"  {level.level:>2}  {level.name:<20} at {level.min_lifetime_xp} XP")

    print()
    print("ACHIEVEMENTS")
    for row in Achievement.query.order_by(Achievement.sort_order).all():
        rule = f"{row.rule_field} >= {row.rule_threshold}" if row.rule_field else row.rule_type
        print(f"  {row.code:<22} {row.name:<26} {rule:<28} +{row.points} XP")

    # Password hashes must be Argon2 and never plaintext.
    print()
    print("PASSWORD STORAGE CHECK")
    for user in User.query.all():
        scheme = user.password_hash.split("$")[1] if user.password_hash.startswith("$") else "PLAINTEXT!"
        print(f"  {user.username:<9} scheme={scheme}")
