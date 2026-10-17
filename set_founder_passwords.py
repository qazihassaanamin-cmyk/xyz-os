"""Set the four founder passwords to simple, known values.

Run with:  python set_founder_passwords.py

The plaintext is never written anywhere; only the Argon2 hash is stored.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app import create_app
from app.extensions import db
from app.services.crypto_service import hash_password

CREDENTIALS = [
    ("ceo@tavryc.com", "hassaan"),
    ("saad@tavryc.com", "saad"),
    ("ilyas@tavryc.com", "ilyas"),
    ("somman@tavryc.com", "somman"),
]


def main() -> None:
    app = create_app("development")
    with app.app_context():
        from app.models.user import User

        for identifier, password in CREDENTIALS:
            user = User.query.filter(db.func.lower(User.email) == identifier.lower()).first()
            if user is None:
                print(f"MISSING: {identifier}")
                continue
            user.password_hash = hash_password(password)
            user.must_change_password = False
            print(f"Password set: {identifier} -> {password}")
        db.session.commit()
    print("Done.")


if __name__ == "__main__":
    main()