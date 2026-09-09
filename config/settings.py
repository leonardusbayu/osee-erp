"""Local development defaults; production configuration must be explicit."""
import os
import secrets
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
# A local .env is data, never executable shell/Python. Process values take priority.
env_file = BASE_DIR / ".env"
if env_file.exists():
    for env_line in env_file.read_text(encoding="utf-8").splitlines():
        env_line = env_line.strip()
        if not env_line or env_line.startswith("#") or "=" not in env_line:
            continue
        env_key, env_value = env_line.split("=", 1)
        env_key = env_key.strip()
        if env_key and env_key.replace("_", "").isalnum():
            os.environ.setdefault(env_key, env_value.strip().strip('"').strip("'"))
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
if DEBUG:
    (BASE_DIR / ".local").mkdir(exist_ok=True)
DEMO_MODE = DEBUG and os.environ.get("OSEE_DEMO_MODE", "1") == "1"
LOCAL_SETUP_ENABLED = DEBUG and os.environ.get("OSEE_LOCAL_SETUP", "1") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
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
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",")
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "django.contrib.humanize",
    "core", "finance", "taxes", "evidence", "imports", "director", "marketing", "webapp",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
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
        "USER": os.environ.get("POSTGRES_USER", "osee"), "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"), "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }}
else:
    if not DEBUG:
        raise ImproperlyConfigured("Production requires PostgreSQL and explicit credentials.")
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / ".local" / "osee.sqlite3", "OPTIONS": {"timeout": 20}}}
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
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / ".local" / "uploads"
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
