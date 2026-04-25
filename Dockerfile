FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Create an unprivileged user. UID/GID 10001 is well above any host-system
# range so it won't collide with bind-mounted host users.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /srv --shell /usr/sbin/nologin app

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY worker ./worker
RUN chown -R app:app /srv

USER app

EXPOSE 5000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5000", "--proxy-headers", "--forwarded-allow-ips", "*"]
