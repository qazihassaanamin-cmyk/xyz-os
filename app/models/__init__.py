"""Model package.

Importing every model module here guarantees that ``db.metadata`` is complete
before Alembic autogenerate runs.
"""

from app.models.achievement import (  # noqa: F401
    Achievement,
    AchievementKind,
    MemberAchievement,
    MemberBadge,
)
from app.models.attachment import Attachment  # noqa: F401
from app.models.audit import AuditLog  # noqa: F401
from app.models.base import StrEnum, TimestampMixin, utcnow  # noqa: F401
from app.models.challenge import (  # noqa: F401
    Challenge,
    ChallengeMetric,
    ChallengeProgress,
)
from app.models.content import (  # noqa: F401
    Announcement,
    AnnouncementRead,
    AnnouncementScope,
    KnowledgeCategory,
    KnowledgeEntry,
    KnowledgeStatus,
)
from app.models.daily import DailyCheckin, DailyReport, DailyReportRevision  # noqa: F401
from app.models.focus import (  # noqa: F401
    FocusSession,
    FocusStatus,
    Workday,
    WorkdayException,
    WorkdayExceptionKind,
)
from app.models.login_attempt import LoginAttempt  # noqa: F401
from app.models.lounge import LoungePost, LoungePostRevision  # noqa: F401
from app.models.notification import Notification, NotificationKind  # noqa: F401
from app.models.project import (  # noqa: F401
    Milestone,
    MilestoneStatus,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectStatus,
)
from app.models.suggestion import (  # noqa: F401
    Suggestion,
    SuggestionCategory,
    SuggestionStatus,
    SuggestionVote,
)
from app.models.task import (  # noqa: F401
    Blocker,
    BlockerKind,
    BlockerStatus,
    Review,
    ReviewDecision,
    Submission,
    SubmissionStatus,
    Subtask,
    Task,
    TaskComment,
    TaskDifficulty,
    TaskPriority,
    TaskStatus,
)
from app.models.user import Invitation, PasswordResetToken, User, UserRole  # noqa: F401
from app.models.xp import (  # noqa: F401
    LevelDefinition,
    Setting,
    XpProfile,
    XpReason,
    XpRule,
    XpTransaction,
)

__all__ = [
    "Achievement",
    "AchievementKind",
    "MemberAchievement",
    "MemberBadge",
    "Attachment",
    "AuditLog",
    "StrEnum",
    "TimestampMixin",
    "utcnow",
    "Challenge",
    "ChallengeMetric",
    "ChallengeProgress",
    "Announcement",
    "AnnouncementRead",
    "AnnouncementScope",
    "KnowledgeCategory",
    "KnowledgeEntry",
    "KnowledgeStatus",
    "DailyCheckin",
    "DailyReport",
    "DailyReportRevision",
    "FocusSession",
    "FocusStatus",
    "Workday",
    "WorkdayException",
    "WorkdayExceptionKind",
    "LoginAttempt",
    "LoungePost",
    "LoungePostRevision",
    "Notification",
    "NotificationKind",
    "Milestone",
    "MilestoneStatus",
    "Project",
    "ProjectMember",
    "ProjectRole",
    "ProjectStatus",
    "Suggestion",
    "SuggestionCategory",
    "SuggestionStatus",
    "SuggestionVote",
    "Blocker",
    "BlockerKind",
    "BlockerStatus",
    "Review",
    "ReviewDecision",
    "Submission",
    "SubmissionStatus",
    "Subtask",
    "Task",
    "TaskComment",
    "TaskDifficulty",
    "TaskPriority",
    "TaskStatus",
    "Invitation",
    "PasswordResetToken",
    "User",
    "UserRole",
    "LevelDefinition",
    "Setting",
    "XpProfile",
    "XpReason",
    "XpRule",
    "XpTransaction",
]
