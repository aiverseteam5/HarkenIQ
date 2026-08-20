FROM python:3.12-slim
WORKDIR /app

# Install base harkeniq package
COPY pyproject.toml setup.cfg* ./
COPY src/ src/
RUN pip install --no-cache-dir -e .

# Install central command service
COPY services/central_command/pyproject.toml services/central_command/
COPY services/central_command/src/ services/central_command/src/
RUN pip install --no-cache-dir -e services/central_command/

EXPOSE 8090
ENTRYPOINT ["python", "-m", "harkeniq_cc"]
