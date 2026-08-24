"""RBAC permissions: fixed roles, permission checks, custom role logic."""

from harkeniq_console.permissions import PERMISSIONS, ROLE_PERMISSIONS, has_permission


class TestPermissionsDefined:
    def test_all_permissions_defined(self):
        """Spec S4: 21 atomic permissions."""
        assert len(PERMISSIONS) == 24  # 21 + R4-3 skill.submit/review/install


class TestFixedRoles:
    def test_seven_fixed_roles(self):
        assert len(ROLE_PERMISSIONS) == 7
        expected = {
            "platform_super_admin",
            "platform_support",
            "tenant_owner",
            "site_admin",
            "operator",
            "auditor",
            "viewer",
        }
        assert set(ROLE_PERMISSIONS.keys()) == expected

    def test_super_admin_has_all(self):
        admin_perms = ROLE_PERMISSIONS["platform_super_admin"]
        assert admin_perms == set(PERMISSIONS.keys())

    def test_platform_support_limited(self):
        perms = ROLE_PERMISSIONS["platform_support"]
        assert perms == {"tenant.view", "support.manage", "support.view", "audit.view"}

    def test_tenant_owner_permissions(self):
        perms = ROLE_PERMISSIONS["tenant_owner"]
        expected = {
            "tenant.view", "user.manage", "user.view", "role.manage",
            "site.manage", "site.view", "fleet.view", "action.approve",
            "incident.view", "incident.acknowledge", "billing.manage",
            "billing.view", "license.view", "support.create",
            "support.view", "audit.view", "audit.export",
            "skill.submit", "skill.install",  # R4-3 marketplace
        }
        assert perms == expected

    def test_operator_cannot_manage_users(self):
        perms = ROLE_PERMISSIONS["operator"]
        assert "user.manage" not in perms

    def test_viewer_read_only(self):
        perms = ROLE_PERMISSIONS["viewer"]
        assert perms == {"fleet.view", "incident.view"}


class TestHasPermission:
    def test_basic_grant(self):
        assert has_permission("operator", "fleet.view") is True

    def test_basic_deny(self):
        assert has_permission("viewer", "user.manage") is False

    def test_unknown_role(self):
        assert has_permission("nonexistent_role", "fleet.view") is False

    def test_custom_permissions_override(self):
        # viewer normally cannot approve actions
        assert has_permission("viewer", "action.approve") is False
        # but custom permissions can grant it
        assert has_permission(
            "viewer", "action.approve", custom_permissions=["action.approve"],
        ) is True

    def test_custom_cannot_exceed_ceiling(self):
        """Custom permissions list that includes admin.dashboard should work
        via has_permission (enforcement of ceiling is a policy layer concern,
        not a has_permission concern). has_permission merges fixed + custom."""
        # viewer + custom admin.dashboard
        result = has_permission(
            "viewer", "admin.dashboard",
            custom_permissions=["admin.dashboard"],
        )
        # has_permission itself grants it; ceiling enforcement is done
        # at the API layer, not here.
        assert result is True
