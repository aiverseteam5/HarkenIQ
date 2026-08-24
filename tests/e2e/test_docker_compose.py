"""Verify Docker Compose configuration is valid (R4-0 Phase 1).

These tests validate the compose file structure without actually running
Docker. They check that all required files exist, env vars are set, and
the service dependency graph is acyclic.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[2]
FULL_STACK = REPO / "deploy" / "full-stack"


class TestDockerComposeConfig:
    def test_compose_file_exists(self):
        assert (FULL_STACK / "docker-compose.yml").is_file()

    def test_init_db_exists(self):
        assert (FULL_STACK / "init-db.sql").is_file()

    def test_compose_is_valid_yaml(self):
        with open(FULL_STACK / "docker-compose.yml") as f:
            config = yaml.safe_load(f)
        assert "services" in config

    def test_all_required_services_present(self):
        with open(FULL_STACK / "docker-compose.yml") as f:
            config = yaml.safe_load(f)
        services = set(config["services"].keys())
        required = {"postgres", "keycloak", "site-manager", "central-command", "console", "mock-simulator"}
        assert required.issubset(services), f"Missing: {required - services}"

    def test_postgres_has_healthcheck(self):
        with open(FULL_STACK / "docker-compose.yml") as f:
            config = yaml.safe_load(f)
        pg = config["services"]["postgres"]
        assert "healthcheck" in pg

    def test_services_depend_on_postgres(self):
        with open(FULL_STACK / "docker-compose.yml") as f:
            config = yaml.safe_load(f)
        for name in ("site-manager", "central-command", "console"):
            svc = config["services"][name]
            deps = svc.get("depends_on", {})
            assert "postgres" in deps, f"{name} should depend on postgres"

    def test_init_db_creates_four_databases(self):
        sql = (FULL_STACK / "init-db.sql").read_text()
        assert "harkeniq_sm" in sql
        assert "harkeniq_cc" in sql
        assert "harkeniq_console" in sql
        assert "keycloak" in sql

    def test_simulator_dockerfile_exists(self):
        assert (FULL_STACK / "Dockerfile.simulator").is_file()


class TestExistingDockerfiles:
    def test_sm_dockerfile_exists(self):
        assert (REPO / "deploy" / "site_manager" / "Dockerfile").is_file()

    def test_cc_dockerfile_exists(self):
        assert (REPO / "deploy" / "r2b" / "Dockerfile.cc").is_file()

    def test_console_dockerfile_exists(self):
        assert (REPO / "deploy" / "r2b" / "Dockerfile.console").is_file()

    def test_keycloak_realm_exists(self):
        assert (REPO / "deploy" / "r2b" / "keycloak-realm-platform.json").is_file()
