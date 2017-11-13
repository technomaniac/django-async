import django
from django.core.handlers.wsgi import WSGIHandler


# def start_event_loop():
#     loop = get_loop()
#
#     # def _stop(signame):
#     #     logger.warning("got signal %s: exit" % signame)
#     #     loop.stop()
#     #
#     # loop.add_signal_handler(getattr(signal, 'SIGINT'), partial(_stop, 'SIGINT'))
#     # loop.add_signal_handler(getattr(signal, 'SIGTERM'), partial(_stop, 'SIGTERM'))
#
#     logger.info("Event loop running forever, press Ctrl+C to interrupt.")
#     logger.info("pid %s: send SIGINT or SIGTERM to exit." % getpid())
#     try:
#         loop.run_forever()
#     except Exception as e:
#         logging.error(str(e))
#     finally:
#         loop.close()


def get_wsgi_application():
    """
    The public interface to Django's WSGI support. Should return a WSGI
    callable.

    Allows us to avoid making django.core.handlers.WSGIHandler public API, in
    case the internal WSGI implementation changes or moves in the future.
    """
    django.setup(set_prefix=False)
    #start_event_loop()
    return WSGIHandler()
