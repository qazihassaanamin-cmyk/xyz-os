"""WSGI entry point.

PythonAnywhere's web tab expects a module-level ``application`` callable in a
WSGI file. Point that file at ``wsgi:application``.

Local development uses the same object:

    flask --app wsgi:app run --debug
"""

from __future__ import annotations

import logging
import os

from app import create_app

app = create_app(os.environ.get("FLASK_ENV", "development"))

# ``application`` is the conventional name for a WSGI callable.
application = app


def _configure_logging() -> None:
    """Log to stdout so PythonAnywhere's error log captures everything."""
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


_configure_logging()


if __name__ == "__main__":  # pragma: no cover - manual run only
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=app.config.get("DEBUG", False))
