import logging
from werkzeug.utils import import_string
from flask import Response, request as _req
from authlib.specs.rfc5849 import (
    AuthorizationServer as _AuthorizationServer,
    TemporaryCredentialMixin,
)
from authlib.common.security import generate_token
from authlib.common.urls import url_encode
from ..cache import Cache

log = logging.getLogger(__name__)


class AuthorizationServer(_AuthorizationServer):
    """Flask implementation of :class:`authlib.rfc5849.AuthorizationServer`.
    Initialize it with a client model class and Flask app instance::

        server = AuthorizationServer(OAuth1Client, app)
        # or initialize lazily
        server = AuthorizationServer(OAuth1Client)
        server.init_app(app)
    """

    def __init__(self, client_model, app=None):
        super(AuthorizationServer, self).__init__(client_model)
        self.app = None
        self.cache = None

        self._hooks = {
            'exists_timestamp_and_nonce': None,
            'create_temporary_credentials_token': None,
            'get_temporary_credentials_token': None,
            'delete_temporary_credentials_token': None,
            'create_authorization_verifier': None,
            'create_token_credentials_token': None,
        }
        self.token_generator = None
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.token_generator = self.create_token_generator(app)
        if app.config.get('OAUTH1_AUTH_CACHE_TYPE'):
            self.cache = Cache(app, config_prefix='OAUTH1_AUTH')

    def register_hook(self, name, func):
        if name not in self._hooks:
            raise ValueError('Invalid "name" of hook')
        self._hooks[name] = func

    def create_token_generator(self, app):
        token_generator = app.config.get('OAUTH1_TOKEN_GENERATOR')

        if isinstance(token_generator, str):
            token_generator = import_string(token_generator)
        else:
            token_generator = lambda: generate_token(42)

        secret_generator = app.config.get('OAUTH1_TOKEN_SECRET_GENERATOR')
        if isinstance(secret_generator, str):
            secret_generator = import_string(secret_generator)
        else:
            secret_generator = lambda: generate_token(48)

        def create_token():
            return {
                'oauth_token': token_generator(),
                'oauth_token_secret': secret_generator()
            }
        return create_token

    def _create_cache_temporary_credentials_token(
            self, token, client_id, redirect_uri):

        key = 'temporary_credential:{}'.format(token['oauth_token'])
        token['oauth_consumer_key'] = client_id
        if redirect_uri:
            token['oauth_callback'] = redirect_uri

        self.cache.set(key, token, timeout=86400)  # cache for one day
        return TemporaryCredential(token)

    def _get_cache_temporary_credentials_token(self, resource_owner_key):
        key = 'temporary_credential:{}'.format(resource_owner_key)
        value = self.cache.get(key)
        if value:
            return TemporaryCredential(value)

    def _delete_cache_temporary_credentials_token(self, resource_owner_key):
        key = 'temporary_credential:{}'.format(resource_owner_key)
        self.cache.delete(key)

    def _create_cache_authorization_verifier(self, token, grant_user, verifier):
        key = 'temporary_credential:{}'.format(token['oauth_token'])
        token['oauth_verifier'] = verifier
        token['user'] = grant_user
        self.cache.set(key, token, timeout=86400)
        return TemporaryCredential(token)

    def exists_timestamp_and_nonce(self, timestamp, nonce, request):
        func = self._hooks['exists_timestamp_and_nonce']
        if callable(func):
            return func(request.client_id, timestamp, nonce)

        if self.cache:
            key = 'nonce:{}-{}-{}'.format(request.client_id, timestamp, nonce)
            rv = self.cache.has(key)
            self.cache.set(key, 1, timeout=self.EXPIRY_TIME)
            return rv

        raise RuntimeError('"exists_timestamp_and_nonce" hook is required.')

    def create_temporary_credentials_token(self, request):
        func = self._hooks['create_temporary_credentials_token']

        if func is None and self.cache:
            func = self._create_cache_temporary_credentials_token

        if callable(func):
            token = self.token_generator()
            return func(token, request.client_id, request.redirect_uri)
        raise RuntimeError(
            '"create_temporary_credentials_token" hook is required.'
        )

    def get_temporary_credentials_token(self, request):
        func = self._hooks['get_temporary_credentials_token']
        if func is None and self.cache:
            func = self._get_cache_temporary_credentials_token

        if callable(func):
            return func(request.resource_owner_key)

        raise RuntimeError(
            '"get_temporary_credentials_token" hook is required.'
        )

    def delete_temporary_credentials_token(self, request):
        func = self._hooks['delete_temporary_credentials_token']
        if func is None and self.cache:
            func = self._delete_cache_temporary_credentials_token

        if callable(func):
            func(request.resource_owner_key)

        raise RuntimeError(
            '"delete_temporary_credentials_token" hook is required.'
        )

    def create_authorization_verifier(self, request):
        func = self._hooks['create_authorization_verifier']
        if func is None and self.cache:
            func = self._create_cache_authorization_verifier

        if callable(func):
            verifier = generate_token(36)
            return func(request.token, request.grant_user, verifier)

        raise RuntimeError(
            '"create_authorization_verifier" hook is required.'
        )

    def create_token_credentials_token(self, request):
        func = self._hooks['create_token_credentials_token']
        if callable(func):
            temporary_credentials = request.token
            token = self.token_generator()
            return func(token, temporary_credentials)

        raise RuntimeError(
            '"create_token_credentials_token" hook is required.'
        )

    def create_temporary_credentials_response(self):
        status, body, headers = self.create_valid_temporary_credentials_response(
            _req.method,
            _req.full_path,
            _req.form.to_dict(flat=True),
            _req.headers
        )
        return Response(url_encode(body), status=status, headers=headers)

    def create_authorization_response(self, grant_user):
        status, body, headers = self.create_valid_authorization_response(
            _req.method,
            _req.full_path,
            _req.form.to_dict(flat=True),
            _req.headers,
            grant_user
        )
        return Response(url_encode(body), status=status, headers=headers)

    def create_token_response(self):
        status, body, headers = self.create_valid_token_response(
            _req.method,
            _req.full_path,
            _req.form.to_dict(flat=True),
            _req.headers,
        )
        return Response(url_encode(body), status=status, headers=headers)


class TemporaryCredential(dict, TemporaryCredentialMixin):
    def get_client_id(self):
        return self.get('oauth_consumer_key')

    def get_redirect_uri(self):
        return self.get('oauth_callback')

    def get_grant_user(self):
        return self.get('user')

    def check_verifier(self, verifier):
        return self.get('oauth_verifier') == verifier

    def get_oauth_token(self):
        return self.get('oauth_token')

    def get_oauth_token_secret(self):
        return self.get('oauth_token_secret')
