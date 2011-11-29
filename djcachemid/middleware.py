"""
Cache middleware. If enabled, each Django-powered page will be cached based on
URL. The canonical way to enable cache middleware is to set
``UpdateCacheMiddleware`` as your first piece of middleware, and
``FetchFromCacheMiddleware`` as the last::

    MIDDLEWARE_CLASSES = [
        'django.middleware.cache.UpdateCacheMiddleware',
        ...
        'django.middleware.cache.FetchFromCacheMiddleware'
    ]

This is counter-intuitive, but correct: ``UpdateCacheMiddleware`` needs to run
last during the response phase, which processes middleware bottom-up;
``FetchFromCacheMiddleware`` needs to run last during the request phase, which
processes middleware top-down.

The single-class ``CacheMiddleware`` can be used for some simple sites.
However, if any other piece of middleware needs to affect the cache key, you'll
need to use the two-part ``UpdateCacheMiddleware`` and
``FetchFromCacheMiddleware``. This'll most often happen when you're using
Django's ``LocaleMiddleware``.

More details about how the caching works:

* Only GET or HEAD-requests with status code 200 are cached.

* The number of seconds each page is stored for is set by the "max-age" section
  of the response's "Cache-Control" header, falling back to the
  CACHE_MIDDLEWARE_SECONDS setting if the section was not found.

* If CACHE_MIDDLEWARE_ANONYMOUS_ONLY is set to True, only anonymous requests
  (i.e., those not made by a logged-in user) will be cached. This is a simple
  and effective way of avoiding the caching of the Django admin (and any other
  user-specific content).

* This middleware expects that a HEAD request is answered with the same response
  headers exactly like the corresponding GET request.

* When a hit occurs, a shallow copy of the original response object is returned
  from process_request.

* Pages will be cached based on the contents of the request headers listed in
  the response's "Vary" header.

* This middleware also sets ETag, Last-Modified, Expires and Cache-Control
  headers on the response object.

"""

import hashlib

from django.utils.encoding import iri_to_uri
from django.conf import settings
from django.core.cache import get_cache, DEFAULT_CACHE_ALIAS
from django.utils.cache import patch_response_headers, get_max_age, cc_delim_re
from django.utils.timezone import get_current_timezone_name
from django.utils.translation import get_language

class TwoPartCacheMiddlewareBase(object):
    @classmethod
    def get_cache_key(cls, request, key_prefix=None, method='GET', cache=None):
        """
        Returns a cache key based on the request path and query. It can be used
        in the request phase because it pulls the list of headers to take into
        account from the global path registry and uses those to build a cache key
        to check against.

        If there is no headerlist stored, the page needs to be rebuilt, so this
        function returns None.
        """
        if key_prefix is None:
            key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        cache_key = cls._generate_cache_header_key(key_prefix, request)
        if cache is None:
            cache = get_cache(settings.CACHE_MIDDLEWARE_ALIAS)
        headerlist = cache.get(cache_key, None)
        if headerlist is not None:
            return cls._generate_cache_key(request, method, headerlist, key_prefix)
        else:
            return None

    @classmethod
    def _i18n_cache_key_suffix(cls, request, cache_key):
        """If necessary, adds the current locale or time zone to the cache key."""
        if settings.USE_I18N or settings.USE_L10N:
            # first check if LocaleMiddleware or another middleware added
            # LANGUAGE_CODE to request, then fall back to the active language
            # which in turn can also fall back to settings.LANGUAGE_CODE
            cache_key += '.%s' % getattr(request, 'LANGUAGE_CODE', get_language())
        if settings.USE_TZ:
            cache_key += '.%s' % get_current_timezone_name()
        return cache_key

    @classmethod
    def _generate_cache_key(cls, request, method, headerlist, key_prefix):
        """Returns a cache key from the headers given in the header list."""
        ctx = hashlib.md5()
        for header in headerlist:
            value = request.META.get(header, None)
            if value is not None:
                ctx.update(value)
        path = hashlib.md5(iri_to_uri(request.get_full_path()))
        cache_key = 'views.decorators.cache.cache_page.%s.%s.%s.%s' % (
            key_prefix, method, path.hexdigest(), ctx.hexdigest())
        return cls._i18n_cache_key_suffix(request, cache_key)

    @classmethod
    def _generate_cache_header_key(cls, key_prefix, request):
        """Returns a cache key for the header cache."""
        path = hashlib.md5(iri_to_uri(request.get_full_path()))
        cache_key = 'views.decorators.cache.cache_header.%s.%s' % (
            key_prefix, path.hexdigest())
        return cls._i18n_cache_key_suffix(request, cache_key)

class UpdateCacheMiddleware(TwoPartCacheMiddlewareBase):
    """
    Response-phase cache middleware that updates the cache if the response is
    cacheable.

    Must be used as part of the two-part update/fetch cache middleware.
    UpdateCacheMiddleware must be the first piece of middleware in
    MIDDLEWARE_CLASSES so that it'll get called last during the response phase.
    """
    def __init__(self):
        self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
        self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        self.cache_anonymous_only = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
        self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS
        self.cache = get_cache(self.cache_alias)

    def _session_accessed(self, request):
        try:
            return request.session.accessed
        except AttributeError:
            return False

    def _should_update_cache(self, request, response):
        if not hasattr(request, '_cache_update_cache') or not request._cache_update_cache:
            return False
        # If the session has not been accessed otherwise, we don't want to
        # cause it to be accessed here. If it hasn't been accessed, then the
        # user's logged-in status has not affected the response anyway.
        if self.cache_anonymous_only and self._session_accessed(request):
            assert hasattr(request, 'user'), "The Django cache middleware with CACHE_MIDDLEWARE_ANONYMOUS_ONLY=True requires authentication middleware to be installed. Edit your MIDDLEWARE_CLASSES setting to insert 'django.contrib.auth.middleware.AuthenticationMiddleware' before the CacheMiddleware."
            if request.user.is_authenticated():
                # Don't cache user-variable requests from authenticated users.
                return False
        return True

    @classmethod
    def learn_cache_key(cls, request, response, cache_timeout=None, key_prefix=None, cache=None):
        """
        Learns what headers to take into account for some request path from the
        response object. It stores those headers in a global path registry so that
        later access to that path will know what headers to take into account
        without building the response object itself. The headers are named in the
        Vary header of the response, but we want to prevent response generation.

        The list of headers to use for cache key generation is stored in the same
        cache as the pages themselves. If the cache ages some data out of the
        cache, this just means that we have to build the response once to get at
        the Vary header and so at the list of headers to use for the cache key.
        """
        if key_prefix is None:
            key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        if cache_timeout is None:
            cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
        cache_key = cls._generate_cache_header_key(key_prefix, request)
        if cache is None:
            cache = get_cache(settings.CACHE_MIDDLEWARE_ALIAS)
        if response.has_header('Vary'):
            headerlist = ['HTTP_'+header.upper().replace('-', '_')
                          for header in cc_delim_re.split(response['Vary'])]
            cache.set(cache_key, headerlist, cache_timeout)
            return cls._generate_cache_key(request, request.method, headerlist, key_prefix)
        else:
            # if there is no Vary header, we still need a cache key
            # for the request.get_full_path()
            cache.set(cache_key, [], cache_timeout)
            return cls._generate_cache_key(request, request.method, [], key_prefix)

    def process_response(self, request, response):
        """Sets the cache, if needed."""
        if not self._should_update_cache(request, response):
            # We don't need to update the cache, just return.
            return response
        if not response.status_code == 200:
            return response
        # Try to get the timeout from the "max-age" section of the "Cache-
        # Control" header before reverting to using the default cache_timeout
        # length.
        timeout = get_max_age(response)
        if timeout == None:
            timeout = self.cache_timeout
        elif timeout == 0:
            # max-age was set to 0, don't bother caching.
            return response
        patch_response_headers(response, timeout)
        if timeout:
            cache_key = self.learn_cache_key(request, response, timeout, self.key_prefix, cache=self.cache)
            if hasattr(response, 'render') and callable(response.render):
                response.add_post_render_callback(
                    lambda r: self.cache.set(cache_key, r, timeout)
                )
            else:
                self.cache.set(cache_key, response, timeout)
        return response

class FetchFromCacheMiddleware(TwoPartCacheMiddlewareBase):
    """
    Request-phase cache middleware that fetches a page from the cache.

    Must be used as part of the two-part update/fetch cache middleware.
    FetchFromCacheMiddleware must be the last piece of middleware in
    MIDDLEWARE_CLASSES so that it'll get called last during the request phase.
    """
    def __init__(self):
        self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
        self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        self.cache_anonymous_only = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
        self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS
        self.cache = get_cache(self.cache_alias)

    def process_request(self, request):
        """
        Checks whether the page is already cached and returns the cached
        version if available.
        """
        if not request.method in ('GET', 'HEAD'):
            request._cache_update_cache = False
            return None # Don't bother checking the cache.

        # try and get the cached GET response
        cache_key = self.get_cache_key(request, self.key_prefix, 'GET', cache=self.cache)
        if cache_key is None:
            request._cache_update_cache = True
            return None # No cache information available, need to rebuild.
        response = self.cache.get(cache_key, None)
        # if it wasn't found and we are looking for a HEAD, try looking just for that
        if response is None and request.method == 'HEAD':
            cache_key = self.get_cache_key(request, self.key_prefix, 'HEAD', cache=self.cache)
            response = self.cache.get(cache_key, None)

        if response is None:
            request._cache_update_cache = True
            return None # No cache information available, need to rebuild.

        # hit, return cached response
        request._cache_update_cache = False
        return response

class CacheMiddleware(UpdateCacheMiddleware, FetchFromCacheMiddleware):
    """
    Cache middleware that provides basic behavior for many simple sites.

    Also used as the hook point for the cache decorator, which is generated
    using the decorator-from-middleware utility.
    """
    def __init__(self, cache_timeout=None, cache_anonymous_only=None, **kwargs):
        # We need to differentiate between "provided, but using default value",
        # and "not provided". If the value is provided using a default, then
        # we fall back to system defaults. If it is not provided at all,
        # we need to use middleware defaults.

        cache_kwargs = {}

        try:
            self.key_prefix = kwargs['key_prefix']
            if self.key_prefix is not None:
                cache_kwargs['KEY_PREFIX'] = self.key_prefix
            else:
                self.key_prefix = ''
        except KeyError:
            self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
            cache_kwargs['KEY_PREFIX'] = self.key_prefix

        try:
            self.cache_alias = kwargs['cache_alias']
            if self.cache_alias is None:
                self.cache_alias = DEFAULT_CACHE_ALIAS
            if cache_timeout is not None:
                cache_kwargs['TIMEOUT'] = cache_timeout
        except KeyError:
            self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS
            if cache_timeout is None:
                cache_kwargs['TIMEOUT'] = settings.CACHE_MIDDLEWARE_SECONDS
            else:
                cache_kwargs['TIMEOUT'] = cache_timeout

        if cache_anonymous_only is None:
            self.cache_anonymous_only = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
        else:
            self.cache_anonymous_only = cache_anonymous_only

        self.cache = get_cache(self.cache_alias, **cache_kwargs)
        self.cache_timeout = self.cache.default_timeout
