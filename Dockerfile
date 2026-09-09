FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_DEBUG=0 OSEE_DEMO_MODE=0 OSEE_LOCAL_SETUP=0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home osee
COPY --chown=osee:osee . .
USER osee
EXPOSE 8000
CMD ["waitress-serve", "--listen=0.0.0.0:8000", "config.wsgi:application"]
