"""Local development defaults; production configuration must be explicit."""
import os
import secrets
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
# A local .env is data, never executable shell/Python. Process values take priority.
env_file = BASE_DIR / ".env"
if os.environ.get("DJANGO_READ_DOT_ENV", "1") == "1" and env_file.exists():
    for env_line in env_file.read_text(encoding="utf-8").splitlines():
        env_line = env_line.strip()
        if not env_line or env_line.startswith("#") or "=" not in env_line:
            continue
        env_key, env_value = env_line.split("=", 1)
        env_key = env_key.strip()
        if env_key and env_key.replace("_", "").isalnum():
            os.environ.setdefault(env_key, env_value.strip().strip('"').strip("'"))
def secret_value(name):
    """Allow mounted Docker secrets without putting credentials in compose output."""
    value = os.environ.get(name, "")
    filename = os.environ.get(name + "_FILE", "")
    if value and filename:
        raise ImproperlyConfigured(f"Configure only {name} or {name}_FILE.")
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ImproperlyConfigured(f"Cannot read the configured {name} file.") from exc
    return value


DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
if DEBUG:
    (BASE_DIR / ".local").mkdir(exist_ok=True)
DEMO_MODE = DEBUG and os.environ.get("OSEE_DEMO_MODE", "1") == "1"
LOCAL_SETUP_ENABLED = DEBUG and os.environ.get("OSEE_LOCAL_SETUP", "1") == "1"
SECRET_KEY = secret_value("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required outside local development.")
    local_dir = BASE_DIR / ".local"
    local_dir.mkdir(exist_ok=True)
    key_path = local_dir / "django-secret"
    if not key_path.exists():
        try:
            with key_path.open("x", encoding="utf-8") as secret_file:
                secret_file.write(secrets.token_urlsafe(64))
        except FileExistsError:
            pass
    SECRET_KEY = key_path.read_text(encoding="utf-8").strip()
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",") if host.strip()]
OTP_ENCRYPTION_KEY = secret_value("OTP_ENCRYPTION_KEY")
MFA_REQUIRED = os.environ.get("OSEE_MFA_REQUIRED", "0" if DEBUG else "1") == "1"
if not DEBUG:
    if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("django-insecure-"):
        raise ImproperlyConfigured("Production requires a generated secret of at least 50 characters.")
    if not os.environ.get("DJANGO_ALLOWED_HOSTS") or not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured("Production requires explicit DJANGO_ALLOWED_HOSTS without a wildcard.")
    try:
        from cryptography.fernet import Fernet
        Fernet(OTP_ENCRYPTION_KEY.encode("ascii"))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ImproperlyConfigured("Production requires a persistent valid OTP_ENCRYPTION_KEY.") from exc
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "django.contrib.humanize",
    "core", "finance", "taxes", "evidence", "imports", "director", "marketing", "webapp",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "core.mfa.AccountSecurityMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware", "core.middleware.SecurityHeadersMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "core.context.app_context",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
if os.environ.get("POSTGRES_DB"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ.get("POSTGRES_USER", "osee"), "PASSWORD": secret_value("POSTGRES_PASSWORD"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"), "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60, "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": 5},
    }}
else:
    if not DEBUG:
        raise ImproperlyConfigured("Production requires PostgreSQL and explicit credentials.")
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": Path(os.environ.get("DJANGO_SQLITE_PATH", str(BASE_DIR / ".local" / "osee.sqlite3"))), "OPTIONS": {"timeout": 20}}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "id"
TIME_ZONE = "Asia/Jakarta"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles")))
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", str(BASE_DIR / ".local" / "uploads")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
WHITENOISE_ALLOW_ALL_ORIGINS = False
if not DEBUG:
    if not DATABASES["default"]["PASSWORD"]:
        raise ImproperlyConfigured("Production PostgreSQL requires a password.")
    if not MEDIA_ROOT.is_absolute() or MEDIA_ROOT.resolve().is_relative_to(STATIC_ROOT.resolve()):
        raise ImproperlyConfigured("Private evidence must use an absolute path outside static files.")
# No MEDIA_URL route: source evidence is available only through permission-checked downloads.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = not DEBUG
# Only the deployment WSGI server interprets proxy headers, after checking the
# exact network peer. Django must never trust arbitrary forwarded headers.
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
SECURE_REDIRECT_EXEMPT = [r"^health/$", r"^ready/$"]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "")
OPENROUTER_PROVIDER = os.environ.get("OPENROUTER_PROVIDER", "")
OPENROUTER_PRIVATE_CONTEXT_ENABLED = os.environ.get("OPENROUTER_PRIVATE_CONTEXT_ENABLED", "0") == "1"
OPENROUTER_MONTHLY_BUDGET_USD = os.environ.get("OPENROUTER_MONTHLY_BUDGET_USD", "5")
OPENROUTER_MAX_REQUEST_COST_USD = os.environ.get("OPENROUTER_MAX_REQUEST_COST_USD", "0")

# Director AI has a separate disclosure policy and credentials from tax guidance.
DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED = os.environ.get("DIRECTOR_AI_PRIVATE_AGGREGATES_ENABLED", "0") == "1"
DIRECTOR_AI_ENDPOINT_APPROVED = os.environ.get("DIRECTOR_AI_ENDPOINT_APPROVED", "0") == "1"
DIRECTOR_OPENROUTER_API_KEY = os.environ.get("DIRECTOR_OPENROUTER_API_KEY", "")
DIRECTOR_OPENROUTER_MODEL = os.environ.get("DIRECTOR_OPENROUTER_MODEL", "")
DIRECTOR_OPENROUTER_PROVIDER = os.environ.get("DIRECTOR_OPENROUTER_PROVIDER", "")
DIRECTOR_AI_MAX_REQUEST_COST_USD = os.environ.get("DIRECTOR_AI_MAX_REQUEST_COST_USD", "0")
DIRECTOR_AI_MONTHLY_BUDGET_USD = os.environ.get("DIRECTOR_AI_MONTHLY_BUDGET_USD", "0")
DIRECTOR_AI_USER_MONTHLY_BUDGET_USD = os.environ.get("DIRECTOR_AI_USER_MONTHLY_BUDGET_USD", DIRECTOR_AI_MONTHLY_BUDGET_USD)

# Marketing has its own owner disclosure; Director consent does not enable it.
MARKETING_AI_PRIVATE_AGGREGATES_ENABLED = os.environ.get("MARKETING_AI_PRIVATE_AGGREGATES_ENABLED", "0") == "1"
