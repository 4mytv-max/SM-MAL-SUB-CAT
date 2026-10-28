# Gunicorn settings for the catalog addon. Gunicorn loads this file
# automatically when the start command does not pass -c. The addon
# is I/O-bound (site, TMDB and PostersPlus fetches): ONE worker with
# several threads keeps poster and catalog requests parallel on the
# free instance (the default single sync worker serialized them, so
# one slow request stalled the whole home screen). The generous
# timeout stops a slow live-row rebuild from getting the worker
# killed at the 30s default, which cold-reset every cache.
worker_class = "gthread"
workers = 1
threads = 8
timeout = 120
