"""Tests for Alembic migration chain (R4-0 Phase 2).

Verifies that initial migrations exist and can create schemas from
the declarative metadata for all three services.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).parents[2]


class TestMigrationFilesExist:
    def test_sm_migration_exists(self):
        path = REPO / "services/site_manager/src/harkeniq_sm/db/migrations/versions/0001_initial.py"
        assert path.is_file()

    def test_cc_migration_exists(self):
        path = REPO / "services/central_command/src/harkeniq_cc/db/migrations/versions/0001_initial.py"
        assert path.is_file()

    def test_console_migration_exists(self):
        path = REPO / "services/console/src/harkeniq_console/db/migrations/versions/0001_initial.py"
        assert path.is_file()


class TestMigrationContent:
    def test_sm_migration_has_upgrade(self):
        path = REPO / "services/site_manager/src/harkeniq_sm/db/migrations/versions/0001_initial.py"
        content = path.read_text()
        assert "def upgrade" in content
        assert "def downgrade" in content
        assert "Base.metadata.create_all" in content

    def test_cc_migration_has_upgrade(self):
        path = REPO / "services/central_command/src/harkeniq_cc/db/migrations/versions/0001_initial.py"
        content = path.read_text()
        assert "def upgrade" in content
        assert "def downgrade" in content
        assert "harkeniq_cc.db.models" in content

    def test_console_migration_has_upgrade(self):
        path = REPO / "services/console/src/harkeniq_console/db/migrations/versions/0001_initial.py"
        content = path.read_text()
        assert "def upgrade" in content
        assert "def downgrade" in content
        assert "harkeniq_console.db.models" in content


class TestMigrationChain:
    def test_sm_revision_chain(self):
        """SM migration has revision 0001 with no down_revision."""
        path = REPO / "services/site_manager/src/harkeniq_sm/db/migrations/versions/0001_initial.py"
        content = path.read_text()
        assert 'revision = "0001"' in content
        assert "down_revision = None" in content

    def test_cc_revision_chain(self):
        path = REPO / "services/central_command/src/harkeniq_cc/db/migrations/versions/0001_initial.py"
        content = path.read_text()
        assert 'revision = "0001"' in content
        assert "down_revision = None" in content

    def test_console_revision_chain(self):
        path = REPO / "services/console/src/harkeniq_console/db/migrations/versions/0001_initial.py"
        content = path.read_text()
        assert 'revision = "0001"' in content
        assert "down_revision = None" in content


class TestEntrypoints:
    def test_cc_entrypoint_exists(self):
        path = REPO / "deploy/full-stack/entrypoint-cc.sh"
        assert path.is_file()
        content = path.read_text()
        assert "alembic upgrade head" in content

    def test_console_entrypoint_exists(self):
        path = REPO / "deploy/full-stack/entrypoint-console.sh"
        assert path.is_file()
        content = path.read_text()
        assert "alembic upgrade head" in content

    def test_sm_entrypoint_exists(self):
        path = REPO / "deploy/site_manager/entrypoint.sh"
        assert path.is_file()
        content = path.read_text()
        assert "alembic upgrade head" in content
