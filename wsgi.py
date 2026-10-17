"""WSGI entry point for PythonAnywhere.

Two ways this file is used:

1. As the repo's ``wsgi.py`` — Flask's CLI imports it to discover commands.
   ``FLASK_ENV`` is then ``development`` and the guard below is skipped, so
   ``flask db migrate`` works without a production secret.

2. As the PythonAnywhere WSGI configuration file, copied to
   ``/var/www/<username>_pythonanywhere_com_wsgi.py``. PA runs it with
   ``FLASK_ENV`` unset, so the guard fires and the app boots in production
   mode with a persistent SECRET_KEY generated on first run.

The repo path is resolved from this file's own location first, then from a
short list of conventional PythonAnywhere locations, so it works whether
the file sits inside the checkout or in ``/var/www``.
"""

import os
import sys

_CANDIDATES = [
    os.path.dirname(os.path.abspath(__file__)),
    os.path.expanduser("~/xyz-os"),
    os.path.expanduser("~/Tavryc"),
    "/home/tavrycinc/xyz-os",
    "/home/tavrycinc/Tavryc",
]

REPO_DIR = next((p for p in _CANDIDATES if os.path.isdir(os.path.join(p, "app"))), _CANDIDATES[0])
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

# PythonAnywhere's free tier has no scheduled tasks and no SMTP, so the app
# works without either. These flags keep behaviour explicit.
os.environ.setdefault("PYTHONANYWHERE_FREE_TIER", "1")

from app import create_app  # noqa: E402

if os.environ.get("FLASK_ENV") == "production":
    application = create_app("production")
else:
    application = create_app("development")