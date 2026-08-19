"""
NetBox configuration for testing NetBox Rack Design.

This configuration is used when running tests and should not be used in production.

Usage:
    export NETBOX_CONFIGURATION=testing.configuration
    python manage.py test netbox_rack_design.tests
"""

import os

# Database configuration
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', 'netbox'),
        'USER': os.getenv('DB_USER', 'netbox'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'netbox'),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '5432'),
        'CONN_MAX_AGE': 300,
    }
}

# Redis configuration
REDIS = {
    'tasks': {
        'HOST': os.getenv('REDIS_HOST', 'localhost'),
        'PORT': int(os.getenv('REDIS_PORT', 6379)),
        'PASSWORD': os.getenv('REDIS_PASSWORD', ''),
        'DATABASE': 0,
        'SSL': False,
    },
    'caching': {
        'HOST': os.getenv('REDIS_HOST', 'localhost'),
        'PORT': int(os.getenv('REDIS_PORT', 6379)),
        'PASSWORD': os.getenv('REDIS_PASSWORD', ''),
        'DATABASE': 1,
        'SSL': False,
    },
}

# Security settings (test-only values)
SECRET_KEY = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
# NetBox 4.5+ hashes API tokens with a keyed pepper and looks the newest one up by
# ID (users.utils.get_current_pepper -> API_TOKEN_PEPPERS.keys()), so this MUST be a
# mapping of id -> value; a list raises AttributeError the first time a token is
# minted. NetBox 4.4 has no v2 tokens and ignores the setting entirely, so the dict
# form is safe across the supported range.
API_TOKEN_PEPPERS = {
    1: 'TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE',
}

# For testing, allow all hosts
ALLOWED_HOSTS = ['*']

# Debug mode stays OFF. Django forces DEBUG=False while running tests anyway, and
# from NetBox 4.5 the bundled django-debug-toolbar (kept in INSTALLED_APPS whenever
# DEBUG is true) fails the system check with debug_toolbar.E001 under tests, which
# aborts the whole run before a single test executes.
DEBUG = False

# Plugin configuration
PLUGINS = [
    'netbox_rack_design',
]

PLUGINS_CONFIG = {
    'netbox_rack_design': {
        # Add any plugin configuration needed for testing
    },
}

# Disable SSL redirect for testing
SECURE_SSL_REDIRECT = False

# Logging configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'netbox_rack_design': {
            'handlers': ['console'],
            'level': 'DEBUG',
        },
    },
}

# RQ (background task) configuration
RQ_QUEUES = {
    'default': {
        'HOST': os.getenv('REDIS_HOST', 'localhost'),
        'PORT': int(os.getenv('REDIS_PORT', 6379)),
        'DB': 0,
        'PASSWORD': os.getenv('REDIS_PASSWORD', ''),
        'SSL': False,
        'DEFAULT_TIMEOUT': 300,
    },
}

# Time zone
TIME_ZONE = 'UTC'

# Internationalization
LANGUAGE_CODE = 'en-us'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = '/tmp/netbox_media'

# Session configuration
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Email configuration (for testing only)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
EMAIL_SERVER = 'localhost'
EMAIL_PORT = 25
EMAIL_TIMEOUT = 10
EMAIL_FROM_EMAIL = 'netbox@localhost'

# NOTE: do NOT set EXEMPT_VIEW_PERMISSIONS = ['*'] here. NetBox's test suites
# assert that users without permission receive 404, and a global exemption makes
# those assertions get 200 instead (CI failure). The suites toggle EXEMPT
# per-test via @override_settings where needed.

# Banner (optional)
BANNER_TOP = ''
BANNER_BOTTOM = ''

# Pagination
PAGINATE_COUNT = 50
MAX_PAGE_SIZE = 1000

# Prefer IPv4 for testing
PREFER_IPV4 = True

# GraphQL
GRAPHQL_ENABLED = True

# Changelog retention
CHANGELOG_RETENTION = 90

# Job result retention
JOBRESULT_RETENTION = 90

# Maps
MAPS_URL = 'https://maps.google.com/?q='

# Remote auth (disabled for testing)
REMOTE_AUTH_ENABLED = False
REMOTE_AUTH_BACKEND = 'netbox.authentication.RemoteUserBackend'
REMOTE_AUTH_HEADER = 'HTTP_REMOTE_USER'
REMOTE_AUTH_AUTO_CREATE_USER = True
REMOTE_AUTH_DEFAULT_GROUPS = []
REMOTE_AUTH_DEFAULT_PERMISSIONS = {}

# Maintenance mode
MAINTENANCE_MODE = False

# Storage
STORAGE_BACKEND = None
STORAGE_CONFIG = {}
