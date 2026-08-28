FROM python:3.12-slim
WORKDIR /app

# Install base harkeniq package
COPY pyproject.toml setup.cfg* ./
COPY src/ src/
RUN pip install --no-cache-dir -e .

# Install central command service (alembic.ini + migrations included:
# the entrypoint runs `alembic upgrade head` before serving — QA-001)
COPY services/central_command/pyproject.toml services/central_command/
COPY services/central_command/alembic.ini services/central_command/
COPY services/central_command/src/ services/central_command/src/
RUN pip install --no-cache-dir -e services/central_command/

COPY deploy/full-stack/entrypoint-cc.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8090
ENTRYPOINT ["/entrypoint.sh"]
