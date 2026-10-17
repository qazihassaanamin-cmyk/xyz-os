"""The four founding members of Tavryc.

These are the official company records: login identifier, role, title and the
responsibilities agreed at launch. The text below is the source of truth that
appears on each founder's profile page.

These identifiers are internal login identifiers. They are not advertised as
working mail inboxes; the application never assumes SMTP delivery to them.
"""

from __future__ import annotations

FOUNDERS: list[dict] = [
    {
        "key": "hassaan",
        "identifier": "ceo@tavryc.com",
        "username": "ceo",
        "full_name": "Muhammad Hassaan",
        "role": "CEO",
        "title": "Caretaker CEO and Technical Lead",
        "title_basis": (
            "Proposed as Caretaker Chief Executive Officer until 31 December 2026, "
            "subject to the required company approvals and appointment documents."
        ),
        "summary": (
            "Runs day-to-day operations and leads technical planning and delivery."
        ),
        "responsibilities": [
            "Coordinating day-to-day operations.",
            "Leading technical planning and development.",
            "Coordinating projects and deadlines.",
            "Helping establish company systems and processes.",
            "Monitoring project progress.",
            "Reporting important matters to the other Founders.",
            "Carrying out decisions approved by the Founders or Board.",
        ],
        "disclaimer": (
            "This internal role and this software do not by themselves constitute a "
            "formal statutory appointment. The appointment must be completed through "
            "the required company process."
        ),
        "avatar_tone": "hassaan",
    },
    {
        "key": "saad",
        "identifier": "saad@tavryc.com",
        "username": "saad",
        "full_name": "Saad Zaheer",
        "role": "MEMBER",
        "title": "Business and Commercial Lead",
        "title_basis": None,
        "summary": (
            "Builds the commercial side: clients, partnerships and revenue."
        ),
        "responsibilities": [
            "Marketing and promotion.",
            "Finding potential clients.",
            "Building business relationships.",
            "Developing partnerships.",
            "Supporting sales and revenue generation.",
            "Identifying commercial opportunities.",
            "Helping establish the Company's public presence.",
        ],
        "disclaimer": None,
        "avatar_tone": "saad",
    },
    {
        "key": "ilyas",
        "identifier": "ilyas@tavryc.com",
        "username": "ilyas",
        "full_name": "Muhammad Ilyas",
        "role": "MEMBER",
        "title": "Company-Wide Oversight Lead",
        "title_basis": None,
        "summary": (
            "Watches progress across the whole company and keeps the founders aligned."
        ),
        "responsibilities": [
            "Monitoring progress across teams.",
            "Coordinating communication between the Founders.",
            "Following up on agreed responsibilities.",
            "Identifying delays and operational problems.",
            "Helping keep business and technical work aligned.",
            "Reporting concerns to the other Founders.",
            "Supporting planning and accountability across the Company.",
        ],
        "disclaimer": (
            "This role does not authorize disregarding Board decisions or acting "
            "beyond the powers approved for it."
        ),
        "avatar_tone": "ilyas",
    },
    {
        "key": "somman",
        "identifier": "somman@tavryc.com",
        "username": "somman",
        "full_name": "Somman Umer",
        "role": "MEMBER",
        "title": "Business Operations and Technical Lead",
        "title_basis": None,
        "summary": (
            "Keeps operations and delivery moving, and contributes to the product."
        ),
        "responsibilities": [
            "Supporting business operations and planning.",
            "Helping coordinate projects and delivery.",
            "Contributing to software and technical development.",
            "Supporting product testing and improvement.",
            "Helping manage project tasks and deadlines.",
            "Working with clients or partners when agreed.",
            "Coordinating with the other Founders on business and technical matters.",
        ],
        "disclaimer": None,
        "avatar_tone": "somman",
    },
]

# Initial projects named in the brief. They are created through the setup
# command, never hardcoded into request handlers, and are ordinary rows the
# CEO can rename, archive or replace afterwards.
INITIAL_PROJECTS: list[dict] = [
    {
        "name": "Raasta",
        "slug": "raasta",
        "description": "First product line. Routing and logistics tooling.",
        "status": "ACTIVE",
        "lead": "hassaan",
        "members": ["hassaan", "ilyas", "somman"],
    },
    {
        "name": "NotesVault",
        "slug": "notesvault",
        "description": "Knowledge capture and document vault.",
        "status": "ACTIVE",
        "lead": "somman",
        "members": ["somman", "hassaan", "ilyas"],
    },
    {
        "name": "Internal Tools",
        "slug": "internal-tools",
        "description": "Company systems and process automation.",
        "status": "ACTIVE",
        "lead": "hassaan",
        "members": ["hassaan", "saad", "ilyas", "somman"],
    },
]


def founder_by_identifier(identifier: str) -> dict | None:
    value = (identifier or "").strip().lower()
    for founder in FOUNDERS:
        if founder["identifier"].lower() == value or founder["username"].lower() == value:
            return founder
    return None


def founder_by_key(key: str) -> dict | None:
    for founder in FOUNDERS:
        if founder["key"] == key:
            return founder
    return None
