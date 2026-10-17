"""Level ladder.

Thresholds are seeded with a complete ladder so the system works from first
launch. Level names use an editorial register appropriate to a small, serious
internal platform rather than game-style language.

Progression is deliberately gentle at the start and widens later: the first
levels are reachable within a week of ordinary verified work, while the top of
the ladder represents months of consistent delivery. At roughly 300-500 XP per
active week, the ladder spans about a year of steady contribution.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.xp import LevelDefinition

LEVEL_LADDER: list[dict] = [
    {"level": 1, "name": "Initiate", "min_lifetime_xp": 0,
     "description": "Access granted. Contribution history begins here."},
    {"level": 2, "name": "Contributor", "min_lifetime_xp": 250,
     "description": "Verified work is landing and being accepted."},
    {"level": 3, "name": "Builder", "min_lifetime_xp": 650,
     "description": "Delivering substantial work with growing independence."},
    {"level": 4, "name": "Operator", "min_lifetime_xp": 1250,
     "description": "Reliable across projects and accountable for delivery."},
    {"level": 5, "name": "Specialist", "min_lifetime_xp": 2100,
     "description": "Recognised depth in a discipline the company depends on."},
    {"level": 6, "name": "Senior Specialist", "min_lifetime_xp": 3300,
     "description": "Sets the standard others build on."},
    {"level": 7, "name": "Principal", "min_lifetime_xp": 4900,
     "description": "Owns outcomes end to end and unblocks the team."},
    {"level": 8, "name": "Founding Architect", "min_lifetime_xp": 7000,
     "description": "Shapes how Tavryc works, not only what it ships."},
    {"level": 9, "name": "Founding Authority", "min_lifetime_xp": 9800,
     "description": "Sustained company-level contribution over the long run."},
    {"level": 10, "name": "Founding Pillar", "min_lifetime_xp": 13000,
     "description": "The highest tier. Earned only through sustained delivery."},
]


def ensure_ladder() -> None:
    """Seed or repair the level ladder. Never overwrites a custom name."""
    existing = {row.level: row for row in LevelDefinition.query.all()}
    changed = False
    for spec in LEVEL_LADDER:
        row = existing.get(spec["level"])
        if row is None:
            db.session.add(LevelDefinition(**spec))
            changed = True
        elif row.min_lifetime_xp != spec["min_lifetime_xp"]:
            # Thresholds are structural; a mismatch would break progression
            # maths, so repair them while leaving the display name alone.
            row.min_lifetime_xp = spec["min_lifetime_xp"]
            changed = True
    if changed:
        try:
            db.session.commit()
        except IntegrityError:  # pragma: no cover
            db.session.rollback()


def _ladder() -> list[LevelDefinition]:
    rows = LevelDefinition.query.order_by(LevelDefinition.level.asc()).all()
    if not rows:
        ensure_ladder()
        rows = LevelDefinition.query.order_by(LevelDefinition.level.asc()).all()
    return rows


def level_for_lifetime_xp(lifetime_xp: int) -> tuple[LevelDefinition, int, int | None]:
    """Return ``(definition, xp_into_level, xp_to_next_level)``.

    ``xp_to_next_level`` is ``None`` at the top of the ladder.
    """
    lifetime_xp = max(0, int(lifetime_xp or 0))
    ladder = _ladder()
    current = ladder[0]
    for row in ladder:
        if lifetime_xp >= row.min_lifetime_xp:
            current = row
        else:
            break

    next_row = next((row for row in ladder if row.level > current.level), None)
    xp_into_level = lifetime_xp - current.min_lifetime_xp
    if next_row is None:
        return current, xp_into_level, None
    return current, xp_into_level, max(0, next_row.min_lifetime_xp - lifetime_xp)


def next_level_definition(current_level: int) -> LevelDefinition | None:
    return (
        LevelDefinition.query.filter(LevelDefinition.level > current_level)
        .order_by(LevelDefinition.level.asc())
        .first()
    )


def definition_for_level(level: int) -> LevelDefinition | None:
    return LevelDefinition.query.filter_by(level=level).first()


def progress_for_lifetime_xp(lifetime_xp: int) -> dict:
    """Everything a progress bar needs, in one call."""
    definition, into_level, to_next = level_for_lifetime_xp(lifetime_xp)
    next_row = next_level_definition(definition.level)
    span = to_next + into_level if to_next is not None else into_level
    percent = 100 if to_next is None else (round(100 * into_level / span) if span else 0)
    return {
        "level": definition.level,
        "name": definition.name,
        "description": definition.description,
        "min_xp": definition.min_lifetime_xp,
        "next_level": next_row.level if next_row else None,
        "next_name": next_row.name if next_row else None,
        "next_min_xp": next_row.min_lifetime_xp if next_row else None,
        "xp_into_level": into_level,
        "xp_to_next_level": to_next,
        "percent": percent,
        "lifetime_xp": lifetime_xp,
        "is_max": next_row is None,
    }
