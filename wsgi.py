"""WSGI entry point for PythonAnywhere.

PythonAnywhere runs the WSGI app named by the WSGI_FILE setting in
/var/www/<username>_tavryc_io/wsgi.py. This file points at the application
factory, so a deploy is: upload the repo, install requirements, run
``flask db upgrade`` and ``flask setup``, then set the environment variables
below.

The production guard only fires when this file is loaded as a WSGI app. When
Flask's CLI imports the module to discover commands, ``FLASK_ENV`` is set to
something other than ``production`` and the guard is skipped, so a developer
can run ``flask db migrate`` without first configuring a production secret.
"""

import os
import sys

# The repo lives at /home/<username>/Tavryc on PythonAnywhere.
REPO_DIR = "/home/%s/Tavryc" % os.environ.get("USER", "")
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