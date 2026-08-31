"""Vercel entry point.

Vercel runs serverless functions, so there is no always-on process to hold a
scheduler. That means on Vercel you get the Pond agent endpoints and /healthz,
while the recurring sweep is driven externally - by the bundled GitHub Actions
workflow (free) or any cron that hits `/runs` with the `scan_now` action.

Set DATABASE_URL to a Postgres URL (neon.tech and supabase.com both have free
tiers). The serverless filesystem is ephemeral, so SQLite state would be lost
between invocations and the bot would re-alert.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Disable the in-process scheduler; serverless has no long-lived process.
os.environ.setdefault("DISABLE_SCHEDULER", "1")

from app.main import app  # noqa: E402

handler = app
