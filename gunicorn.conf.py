# Stop active media work as soon as Docker sends SIGTERM, before Gunicorn waits
# for streaming requests to finish. Keep the grace period below docker stop's
# default ten-second deadline.
bind = '0.0.0.0:8080'
workers = 1
threads = 8
timeout = 0
graceful_timeout = 5


def post_worker_init(worker):
    from app import media_runtime
    media_runtime.install_signal_handlers()


def worker_exit(server, worker):
    import sys
    application = sys.modules.get('app')
    if application is not None:
        application.media_runtime.shutdown()
