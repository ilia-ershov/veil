"""gunicorn settings for the container: gunicorn --config gunicorn.conf.py wsgi:app"""

import logging
import os

bind = "0.0.0.0:5000"
workers = int(os.getenv("WEB_CONCURRENCY") or 3)  # compose passes an unset variable as ""
accesslog = "-"

# The workers' heartbeat file lives in memory. On the container's disk-backed /tmp a busy disk
# stalls the heartbeat, and gunicorn kills healthy workers ("WORKER TIMEOUT").
worker_tmp_dir = "/dev/shm"  # noqa: S108 (gunicorn uses mkstemp there and unlinks the file at once)

# No control socket for `gunicornc`: nothing uses it, and gunicorn would put it in the home
# directory, which the app user does not have ("Control server error: Permission denied").
control_socket_disable = True


class _HideHealthChecks(logging.Filter):
    """Docker's health check asks /api/health every 30 seconds: log it only when it fails."""

    def filter(self, record):
        line = record.getMessage()
        return not ('"GET /api/health ' in line and '" 200 ' in line)


def on_starting(server):
    logging.getLogger("gunicorn.access").addFilter(_HideHealthChecks())
