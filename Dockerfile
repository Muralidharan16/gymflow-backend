ARG PYTHON_IMAGE=python:3.12.14-alpine3.24@sha256:c4634f578a412db396771b61b064c6e546c9d6414c7fb5b1b05d5871f1885f7b

FROM ${PYTHON_IMAGE} AS python-deps
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv "$VIRTUAL_ENV" \
    && python -m pip install --no-cache-dir 'pip==26.2.1'
COPY requirements-test.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
    && python -m pip check \
    && rm -rf \
       "$VIRTUAL_ENV/lib/python3.12/site-packages/pip" \
       "$VIRTUAL_ENV/lib/python3.12/site-packages"/pip-*.dist-info \
       "$VIRTUAL_ENV/lib/python3.12/site-packages"/setuptools* \
       "$VIRTUAL_ENV/lib/python3.12/site-packages/_distutils_hack" \
       "$VIRTUAL_ENV/bin"/pip*

FROM ${PYTHON_IMAGE} AS runtime
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
RUN rm -rf \
      /usr/local/lib/python3.12/site-packages/pip \
      /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
      /usr/local/lib/python3.12/site-packages/setuptools* \
      /usr/local/lib/python3.12/site-packages/_distutils_hack \
      /usr/local/bin/pip*
WORKDIR /app
COPY --from=python-deps /opt/venv /opt/venv
COPY . /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["wget","-q","-T","2","-O","/dev/null","http://127.0.0.1:8000/_system/ready"]
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
