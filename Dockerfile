FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_READ_DOT_ENV=0 DJANGO_DEBUG=0 OSEE_DEMO_MODE=0 OSEE_LOCAL_SETUP=0 DJANGO_MEDIA_ROOT=/app/private/uploads
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 && rm -rf /var/lib/apt/lists/* && useradd --uid 1000 --create-home osee
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=osee:osee . .
RUN mkdir -p /app/private/uploads /app/staticfiles /app/.local && chown -R osee:osee /app/private /app/staticfiles /app/.local
USER osee
RUN DJANGO_DEBUG=1 DJANGO_SECRET_KEY=static-build-only-not-a-runtime-secret python manage.py collectstatic --noinput
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD python deploy/healthcheck.py
CMD ["python", "deploy/serve.py"]
