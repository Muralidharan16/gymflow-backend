ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS python-deps
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv "$VIRTUAL_ENV" \
    && python -m pip install --no-cache-dir 'pip==26.2.1'
COPY requirements-test.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
    && python -m pip check

FROM ${PYTHON_IMAGE} AS runtime
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
RUN groupadd --gid 10001 doers \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin doers
WORKDIR /app
COPY --from=python-deps /opt/venv /opt/venv
COPY . /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python","-c","import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/_system/ready',timeout=2); raise SystemExit(0 if r.status == 200 else 1)"]
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
