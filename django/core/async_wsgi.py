import asyncio
import logging
import sys
import types

from django_wsgi.handler import DjangoApplication

from django import http
from django.conf import settings
from django.core import signals, urlresolvers
from django.core.exceptions import (
    PermissionDenied, SuspiciousOperation,
)
from django.core.handlers.wsgi import get_script_name
from django.core.urlresolvers import set_script_prefix
from django.http.multipartparser import MultiPartParserError
from django.utils.encoding import force_str
from django.utils.encoding import force_text
from django.views import debug

logger = logging.getLogger('django.request')


# must be ran with a server handler that understands asyncio.future
class AsyncDjangoApplication(DjangoApplication):
    # this would work in django 1.10 but this method doesn't exist in 1.9

    async def _get_response(self, request):
        response = super(AsyncDjangoApplication, self)._get_response(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response

    # so instead for django 1.9 we have to reimplement this big method just to change two lines
    async def get_response(self, request):
        "Returns an HttpResponse object for the given HttpRequest"

        # Setup default url resolver for this thread, this code is outside
        # the try/except so we don't get a spurious "unbound local
        # variable" exception in the event an exception is raised before
        # resolver is set
        urlconf = settings.ROOT_URLCONF
        urlresolvers.set_urlconf(urlconf)
        resolver = urlresolvers.get_resolver(urlconf)
        # Use a flag to check if the response was rendered to prevent
        # multiple renderings or to force rendering if necessary.
        response_is_rendered = False
        try:
            response = None
            # Apply request middleware
            for middleware_method in self._request_middleware:
                response = middleware_method(request)
                if response:
                    break

            if response is None:
                if hasattr(request, 'urlconf'):
                    # Reset url resolver with a custom URLconf.
                    urlconf = request.urlconf
                    urlresolvers.set_urlconf(urlconf)
                    resolver = urlresolvers.get_resolver(urlconf)

                resolver_match = resolver.resolve(request.path_info)
                callback, callback_args, callback_kwargs = resolver_match
                request.resolver_match = resolver_match

                # Apply view middleware
                for middleware_method in self._view_middleware:
                    response = middleware_method(request, callback, callback_args, callback_kwargs)
                    if response:
                        break

            if response is None:
                wrapped_callback = self.make_view_atomic(callback)
                try:
                    response = wrapped_callback(request, *callback_args, **callback_kwargs)
                    # THE TWO LINES NEEDED
                    # check if view returned a coroutine and get the async result
                    if asyncio.iscoroutine(response):
                        response = await response
                except Exception as e:
                    response = self.process_exception_by_middleware(e, request)

            # Complain if the view returned None (a common error).
            if response is None:
                if isinstance(callback, types.FunctionType):  # FBV
                    view_name = callback.__name__
                else:  # CBV
                    view_name = callback.__class__.__name__ + '.__call__'
                raise ValueError("The view %s.%s didn't return an HttpResponse object. It returned None instead."
                                 % (callback.__module__, view_name))

            # If the response supports deferred rendering, apply template
            # response middleware and then render the response
            if hasattr(response, 'render') and callable(response.render):
                for middleware_method in self._template_response_middleware:
                    response = middleware_method(request, response)
                    # Complain if the template response middleware returned None (a common error).
                    if response is None:
                        raise ValueError(
                            "%s.process_template_response didn't return an "
                            "HttpResponse object. It returned None instead."
                            % (middleware_method.__self__.__class__.__name__))
                try:
                    response = response.render()
                except Exception as e:
                    response = self.process_exception_by_middleware(e, request)

                response_is_rendered = True

        except http.Http404 as exc:
            logger.warning('Not Found: %s', request.path,
                           extra={
                               'status_code': 404,
                               'request': request
                           })
            if settings.DEBUG:
                response = debug.technical_404_response(request, exc)
            else:
                response = self.get_exception_response(request, resolver, 404, exc)

        except PermissionDenied as exc:
            logger.warning(
                'Forbidden (Permission denied): %s', request.path,
                extra={
                    'status_code': 403,
                    'request': request
                })
            response = self.get_exception_response(request, resolver, 403, exc)

        except MultiPartParserError as exc:
            logger.warning(
                'Bad request (Unable to parse request body): %s', request.path,
                extra={
                    'status_code': 400,
                    'request': request
                })
            response = self.get_exception_response(request, resolver, 400, exc)

        except SuspiciousOperation as exc:
            # The request logger receives events for any problematic request
            # The security logger receives events for all SuspiciousOperations
            security_logger = logging.getLogger('django.security.%s' %
                                                exc.__class__.__name__)
            security_logger.error(
                force_text(exc),
                extra={
                    'status_code': 400,
                    'request': request
                })
            if settings.DEBUG:
                return debug.technical_500_response(request, *sys.exc_info(), status_code=400)

            response = self.get_exception_response(request, resolver, 400, exc)

        except SystemExit:
            # Allow sys.exit() to actually exit. See tickets #1023 and #4701
            raise

        except:  # Handle everything else.
            # Get the exception info now, in case another exception is thrown later.
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        try:
            # Apply response middleware, regardless of the response
            for middleware_method in self._response_middleware:
                response = middleware_method(request, response)
                # Complain if the response middleware returned None (a common error).
                if response is None:
                    raise ValueError(
                        "%s.process_response didn't return an "
                        "HttpResponse object. It returned None instead."
                        % (middleware_method.__self__.__class__.__name__))
            response = self.apply_response_fixes(request, response)
        except:  # Any exception should be gathered and handled
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        response._closable_objects.append(request)

        # If the exception handler returns a TemplateResponse that has not
        # been rendered, force it to be rendered.
        if not response_is_rendered and callable(getattr(response, 'render', None)):
            response = response.render()

        return response

    @asyncio.coroutine
    def __call__(self, environ, start_response):
        # Set up middleware if needed. We couldn't do this earlier, because
        # settings weren't available.
        if self._request_middleware is None:
            with self.initLock:
                try:
                    # Check that middleware is still uninitialized.
                    if self._request_middleware is None:
                        self.load_middleware()
                except:
                    # Unload whatever middleware we got
                    self._request_middleware = None
                    raise

        set_script_prefix(get_script_name(environ))
        signals.request_started.send(sender=self.__class__, environ=environ)
        try:
            request = self.request_class(environ)
        except UnicodeDecodeError:
            logger.warning(
                'Bad Request (UnicodeDecodeError)',
                exc_info=sys.exc_info(),
                extra={
                    'status_code': 400,
                }
            )
            response = http.HttpResponseBadRequest()
        else:
            response = self.get_response(request)

        if asyncio.iscoroutine(response):
            response = yield from response

        response._handler_class = self.__class__

        status = '%d %s' % (response.status_code, response.reason_phrase)
        logger.debug('Response: {0} {1}'.format(status, environ['PATH_INFO']))
        response_headers = [(str(k), str(v)) for k, v in response.items()]
        for c in response.cookies.values():
            response_headers.append((str('Set-Cookie'), str(c.output(header=''))))
        start_response(force_str(status), response_headers)
        if getattr(response, 'file_to_stream', None) is not None and environ.get('wsgi.file_wrapper'):
            response = environ['wsgi.file_wrapper'](response.file_to_stream)
        return response
