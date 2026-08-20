import { Fragment, type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import ConfirmDialog from "../components/ConfirmDialog";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { getJson, postJson, patchJson, deleteJson } from "../api";
import { useAuth } from "../useAuth";
import type { PaginatedResponse, User } from "../types";

/* ── Types ──────────────────────────────────────── */

interface CustomRole {
  id: string;
  name: string;
  permissions: string[];
  created_by: string;
  created_at: string;
}

interface PermissionDef {
  key: string;
  label: string;
  domain: string;
}

/* ── Constants ──────────────────────────────────── */

const PAGE_SIZE = 20;
const POLL_INTERVAL = 30000;

const FIXED_ROLES = [
  { value: "platform_admin", label: "Platform Admin" },
  { value: "tenant_owner", label: "Tenant Owner" },
  { value: "site_admin", label: "Site Admin" },
  { value: "operator", label: "Operator" },
  { value: "auditor", label: "Auditor" },
  { value: "viewer", label: "Viewer" },
  { value: "billing_admin", label: "Billing Admin" },
];

const ASSIGNABLE_ROLES = [
  { value: "tenant_owner", label: "Tenant Owner" },
  { value: "site_admin", label: "Site Admin" },
  { value: "operator", label: "Operator" },
  { value: "auditor", label: "Auditor" },
  { value: "viewer", label: "Viewer" },
];

const USER_STATUS_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  active: "success",
  invited: "info",
  disabled: "critical",
};

const ROLE_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  platform_admin: "critical",
  tenant_owner: "warning",
  site_admin: "info",
  operator: "success",
  auditor: "neutral",
  viewer: "neutral",
  billing_admin: "warning",
};

const ALL_PERMISSIONS: PermissionDef[] = [
  { key: "tenant.manage", label: "Manage", domain: "Tenant" },
  { key: "tenant.view", label: "View", domain: "Tenant" },
  { key: "user.manage", label: "Manage", domain: "Users" },
  { key: "user.view", label: "View", domain: "Users" },
  { key: "role.manage", label: "Manage", domain: "Roles" },
  { key: "site.manage", label: "Manage", domain: "Sites" },
  { key: "site.view", label: "View", domain: "Sites" },
  { key: "fleet.view", label: "View", domain: "Fleet" },
  { key: "action.approve", label: "Approve", domain: "Actions" },
  { key: "incident.view", label: "View", domain: "Incidents" },
  { key: "incident.acknowledge", label: "Acknowledge", domain: "Incidents" },
  { key: "billing.manage", label: "Manage", domain: "Billing" },
  { key: "billing.view", label: "View", domain: "Billing" },
  { key: "license.manage", label: "Manage", domain: "Licenses" },
  { key: "license.view", label: "View", domain: "Licenses" },
  { key: "support.create", label: "Create", domain: "Support" },
  { key: "support.view", label: "View", domain: "Support" },
  { key: "audit.view", label: "View", domain: "Audit" },
  { key: "audit.export", label: "Export", domain: "Audit" },
];

const PERMISSION_DOMAINS = [...new Set(ALL_PERMISSIONS.map((p) => p.domain))];

/* ── Default role permission mapping (for the matrix) ── */
const ROLE_PERMISSIONS: Record<string, string[]> = {
  platform_admin: ALL_PERMISSIONS.map((p) => p.key),
  tenant_owner: ALL_PERMISSIONS.map((p) => p.key),
  site_admin: [
    "tenant.view", "user.view", "site.manage", "site.view", "fleet.view",
    "action.approve", "incident.view", "incident.acknowledge", "license.view",
    "support.create", "support.view", "audit.view",
  ],
  operator: [
    "tenant.view", "site.view", "fleet.view", "action.approve",
    "incident.view", "incident.acknowledge", "support.create", "support.view",
  ],
  auditor: [
    "tenant.view", "user.view", "site.view", "fleet.view", "incident.view",
    "billing.view", "license.view", "support.view", "audit.view", "audit.export",
  ],
  viewer: ["tenant.view", "site.view", "fleet.view", "incident.view", "support.view"],
  billing_admin: [
    "tenant.view", "billing.manage", "billing.view", "license.manage",
    "license.view", "support.create", "support.view",
  ],
};

/* ── Styles ─────────────────────────────────────── */

const tabBar: CSSProperties = {
  display: "flex",
  gap: "0",
  borderBottom: "2px solid var(--border-color)",
  marginBottom: "1.25rem",
};

const tabBtn = (active: boolean): CSSProperties => ({
  padding: "0.625rem 1.25rem",
  fontSize: "0.875rem",
  fontWeight: active ? 600 : 400,
  color: active ? "var(--accent)" : "var(--text-secondary)",
  background: "none",
  border: "none",
  borderBottom: active ? "2px solid var(--accent)" : "2px solid transparent",
  marginBottom: "-2px",
  cursor: "pointer",
  transition: "color var(--transition), border-color var(--transition)",
});

const modalOverlay: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--bg-overlay)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 2000,
};

const modalCard: CSSProperties = {
  background: "var(--bg-card)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-lg)",
  padding: "1.5rem",
  maxWidth: 540,
  width: "90vw",
  maxHeight: "85vh",
  overflow: "auto",
};

const formGroup: CSSProperties = { marginBottom: "1rem" };

const labelStyle: CSSProperties = {
  display: "block",
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  marginBottom: "0.375rem",
};

const errorText: CSSProperties = {
  fontSize: "0.75rem",
  color: "var(--status-critical)",
  marginTop: "0.25rem",
};

const sectionTitle: CSSProperties = {
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  marginBottom: "0.75rem",
  marginTop: "1.25rem",
  borderBottom: "1px solid var(--border-light)",
  paddingBottom: "0.375rem",
};

const detailRow: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  padding: "0.375rem 0",
  fontSize: "0.8125rem",
  borderBottom: "1px solid var(--border-light)",
};

const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = { color: "var(--text-primary)", fontWeight: 500, textAlign: "right" };

const permCheckbox: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.25rem 0",
  fontSize: "0.8125rem",
};

const domainHeader: CSSProperties = {
  fontSize: "0.75rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  marginTop: "0.75rem",
  marginBottom: "0.375rem",
};

const matrixTable: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.75rem",
};

const matrixTh: CSSProperties = {
  padding: "0.5rem 0.625rem",
  fontWeight: 600,
  fontSize: "0.6875rem",
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  borderBottom: "2px solid var(--border-color)",
  background: "var(--bg-primary)",
  position: "sticky",
  top: 0,
  textAlign: "center",
  whiteSpace: "nowrap",
};

const matrixTd: CSSProperties = {
  padding: "0.375rem 0.625rem",
  borderBottom: "1px solid var(--border-light)",
  textAlign: "center",
};

const checkmark: CSSProperties = {
  color: "var(--status-ok)",
  fontWeight: 700,
  fontSize: "0.875rem",
};

/* ── Helpers ────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function roleLabel(role: string): string {
  const found = FIXED_ROLES.find((r) => r.value === role);
  return found ? found.label : role;
}

/* ── Component ──────────────────────────────────── */

export default function UserManagement() {
  const { toasts, toast, dismiss } = useToast();
  const { user: authUser } = useAuth();
  const tenantId = authUser?.tenant_id ?? "";

  /* ── Tabs ────────────────────────────────────── */
  const [activeTab, setActiveTab] = useState<"users" | "roles" | "matrix">("users");

  /* ── Users list state ────────────────────────── */
  const [users, setUsers] = useState<User[]>([]);
  const [usersTotal, setUsersTotal] = useState(0);
  const [usersPage, setUsersPage] = useState(1);
  const [usersLoading, setUsersLoading] = useState(true);
  const [userFilters, setUserFilters] = useState<Record<string, string>>({
    search: "",
    role: "",
    status: "",
  });

  /* ── Custom roles state ──────────────────────── */
  const [customRoles, setCustomRoles] = useState<CustomRole[]>([]);
  const [rolesLoading, setRolesLoading] = useState(true);

  /* ── User detail state ───────────────────────── */
  const [selectedUser, setSelectedUser] = useState<User | null>(null);
  const [userPermissions, setUserPermissions] = useState<string[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  /* ── Invite user state ───────────────────────── */
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteLoading, setInviteLoading] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("viewer");
  const [inviteCustomRoles, setInviteCustomRoles] = useState<string[]>([]);
  const [inviteErrors, setInviteErrors] = useState<Record<string, string>>({});

  /* ── Edit user state ─────────────────────────── */
  const [editOpen, setEditOpen] = useState(false);
  const [editLoading, setEditLoading] = useState(false);
  const [editRole, setEditRole] = useState("");
  const [editStatus, setEditStatus] = useState("");

  /* ── Disable user state ──────────────────────── */
  const [disableOpen, setDisableOpen] = useState(false);
  const [disableLoading, setDisableLoading] = useState(false);
  const [disableTarget, setDisableTarget] = useState<User | null>(null);

  /* ── Create/edit role state ──────────────────── */
  const [roleDialogOpen, setRoleDialogOpen] = useState(false);
  const [roleDialogMode, setRoleDialogMode] = useState<"create" | "edit">("create");
  const [roleDialogLoading, setRoleDialogLoading] = useState(false);
  const [roleName, setRoleName] = useState("");
  const [rolePermissions, setRolePermissions] = useState<string[]>([]);
  const [editingRole, setEditingRole] = useState<CustomRole | null>(null);
  const [roleErrors, setRoleErrors] = useState<Record<string, string>>({});

  /* ── Delete role state ───────────────────────── */
  const [deleteRoleOpen, setDeleteRoleOpen] = useState(false);
  const [deleteRoleLoading, setDeleteRoleLoading] = useState(false);
  const [deleteRoleTarget, setDeleteRoleTarget] = useState<CustomRole | null>(null);

  /* ── Fetch users ─────────────────────────────── */
  const fetchUsers = useCallback(async () => {
    if (!tenantId) return;
    try {
      const params = new URLSearchParams();
      if (userFilters.search) params.set("search", userFilters.search);
      if (userFilters.role) params.set("role", userFilters.role);
      if (userFilters.status) params.set("status", userFilters.status);
      params.set("page", String(usersPage));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<PaginatedResponse<User>>(
        `/api/tenants/${tenantId}/users?${params.toString()}`,
      );
      setUsers(res.items);
      setUsersTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load users", "error");
    } finally {
      setUsersLoading(false);
    }
  }, [tenantId, userFilters, usersPage, toast]);

  useEffect(() => {
    if (activeTab === "users") {
      setUsersLoading(true);
      void fetchUsers();
    }
  }, [fetchUsers, activeTab]);

  useEffect(() => {
    if (activeTab !== "users") return;
    const timer = setInterval(() => void fetchUsers(), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchUsers, activeTab]);

  /* ── Fetch custom roles ──────────────────────── */
  const fetchRoles = useCallback(async () => {
    if (!tenantId) return;
    try {
      const res = await getJson<CustomRole[]>(`/api/tenants/${tenantId}/roles`);
      setCustomRoles(res);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load roles", "error");
    } finally {
      setRolesLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => {
    if (activeTab === "roles" || activeTab === "matrix") {
      setRolesLoading(true);
      void fetchRoles();
    }
  }, [fetchRoles, activeTab]);

  /* ── Open user detail ────────────────────────── */
  const openUserDetail = useCallback(
    async (u: User) => {
      setDetailOpen(true);
      setDetailLoading(true);
      try {
        const detail = await getJson<User>(`/api/tenants/${tenantId}/users/${u.id}`);
        setSelectedUser(detail);
        // Try to fetch effective permissions
        try {
          const perms = await getJson<string[]>(
            `/api/tenants/${tenantId}/users/permissions/list`,
          );
          setUserPermissions(perms);
        } catch {
          // Fallback: compute from the role
          const rolePerms = ROLE_PERMISSIONS[detail.role] ?? [];
          setUserPermissions(rolePerms);
        }
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load user detail", "error");
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [tenantId, toast],
  );

  /* ── Invite user ─────────────────────────────── */
  const validateInvite = useCallback((): boolean => {
    const errors: Record<string, string> = {};
    if (!inviteEmail.trim()) {
      errors.email = "Email is required";
    } else if (!isValidEmail(inviteEmail)) {
      errors.email = "Invalid email format";
    }
    if (!inviteRole) errors.role = "Role is required";
    setInviteErrors(errors);
    return Object.keys(errors).length === 0;
  }, [inviteEmail, inviteRole]);

  const handleInvite = useCallback(async () => {
    if (!validateInvite()) return;
    setInviteLoading(true);
    try {
      await postJson(`/api/tenants/${tenantId}/users/invite`, {
        email: inviteEmail,
        role: inviteRole,
        custom_role_ids: inviteCustomRoles,
      });
      toast("User invited successfully", "success");
      setInviteOpen(false);
      setInviteEmail("");
      setInviteRole("viewer");
      setInviteCustomRoles([]);
      setInviteErrors({});
      void fetchUsers();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to invite user", "error");
    } finally {
      setInviteLoading(false);
    }
  }, [tenantId, inviteEmail, inviteRole, inviteCustomRoles, validateInvite, toast, fetchUsers]);

  /* ── Edit user ───────────────────────────────── */
  const openEditUser = useCallback((u: User) => {
    setSelectedUser(u);
    setEditRole(u.role);
    setEditStatus(u.status);
    setEditOpen(true);
  }, []);

  const handleEditUser = useCallback(async () => {
    if (!selectedUser) return;
    setEditLoading(true);
    try {
      await patchJson(`/api/tenants/${tenantId}/users/${selectedUser.id}`, {
        role: editRole,
        status: editStatus,
      });
      toast("User updated successfully", "success");
      setEditOpen(false);
      setSelectedUser(null);
      void fetchUsers();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to update user", "error");
    } finally {
      setEditLoading(false);
    }
  }, [tenantId, selectedUser, editRole, editStatus, toast, fetchUsers]);

  /* ── Disable user ────────────────────────────── */
  const handleDisableUser = useCallback(async () => {
    if (!disableTarget) return;
    setDisableLoading(true);
    try {
      await deleteJson(`/api/tenants/${tenantId}/users/${disableTarget.id}`);
      toast("User disabled", "success");
      setDisableOpen(false);
      setDisableTarget(null);
      void fetchUsers();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to disable user", "error");
    } finally {
      setDisableLoading(false);
    }
  }, [tenantId, disableTarget, toast, fetchUsers]);

  /* ── Create / Edit role ──────────────────────── */
  const openCreateRole = useCallback(() => {
    setRoleDialogMode("create");
    setRoleName("");
    setRolePermissions([]);
    setEditingRole(null);
    setRoleErrors({});
    setRoleDialogOpen(true);
  }, []);

  const openEditRole = useCallback((role: CustomRole) => {
    setRoleDialogMode("edit");
    setRoleName(role.name);
    setRolePermissions([...role.permissions]);
    setEditingRole(role);
    setRoleErrors({});
    setRoleDialogOpen(true);
  }, []);

  const validateRole = useCallback((): boolean => {
    const errors: Record<string, string> = {};
    if (!roleName.trim()) errors.name = "Role name is required";
    if (rolePermissions.length === 0) errors.permissions = "Select at least one permission";
    setRoleErrors(errors);
    return Object.keys(errors).length === 0;
  }, [roleName, rolePermissions]);

  const handleSaveRole = useCallback(async () => {
    if (!validateRole()) return;
    setRoleDialogLoading(true);
    try {
      if (roleDialogMode === "create") {
        await postJson(`/api/tenants/${tenantId}/roles`, {
          name: roleName,
          permissions: rolePermissions,
        });
        toast("Custom role created", "success");
      } else if (editingRole) {
        await patchJson(`/api/tenants/${tenantId}/roles/${editingRole.id}`, {
          name: roleName,
          permissions: rolePermissions,
        });
        toast("Custom role updated", "success");
      }
      setRoleDialogOpen(false);
      void fetchRoles();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to save role", "error");
    } finally {
      setRoleDialogLoading(false);
    }
  }, [tenantId, roleDialogMode, roleName, rolePermissions, editingRole, validateRole, toast, fetchRoles]);

  const togglePermission = useCallback((perm: string) => {
    setRolePermissions((prev) =>
      prev.includes(perm) ? prev.filter((p) => p !== perm) : [...prev, perm],
    );
  }, []);

  /* ── Delete role ─────────────────────────────── */
  const handleDeleteRole = useCallback(async () => {
    if (!deleteRoleTarget) return;
    setDeleteRoleLoading(true);
    try {
      await deleteJson(`/api/tenants/${tenantId}/roles/${deleteRoleTarget.id}`);
      toast("Custom role deleted", "success");
      setDeleteRoleOpen(false);
      setDeleteRoleTarget(null);
      void fetchRoles();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to delete role", "error");
    } finally {
      setDeleteRoleLoading(false);
    }
  }, [tenantId, deleteRoleTarget, toast, fetchRoles]);

  /* ── User filter definitions ─────────────────── */
  const userFilterDefs = useMemo<FilterDef[]>(
    () => [
      { key: "search", label: "Search", type: "text", placeholder: "Search users..." },
      {
        key: "role",
        label: "Role",
        type: "select",
        options: FIXED_ROLES,
      },
      {
        key: "status",
        label: "Status",
        type: "select",
        options: [
          { value: "active", label: "Active" },
          { value: "invited", label: "Invited" },
          { value: "disabled", label: "Disabled" },
        ],
      },
    ],
    [],
  );

  /* ── Users columns ───────────────────────────── */
  const userColumns = useMemo<Column<User>[]>(
    () => [
      { key: "email", header: "Email", sortKey: "email" },
      { key: "name", header: "Display Name", sortKey: "name" },
      {
        key: "role",
        header: "Role",
        render: (r) => (
          <StatusBadge
            status={roleLabel(r.role)}
            variant={ROLE_VARIANT[r.role] ?? "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "status",
        header: "Status",
        render: (r) => (
          <StatusBadge
            status={r.status}
            variant={USER_STATUS_VARIANT[r.status] ?? "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "created_at",
        header: "Created",
        sortKey: "created_at",
        render: (r) => formatDate(r.created_at),
      },
      {
        key: "last_login_at",
        header: "Last Login",
        render: (r) => formatDateTime(r.last_login_at),
      },
    ],
    [],
  );

  const userRowActions = useMemo(
    () => [
      {
        label: "Edit",
        onClick: (row: User) => openEditUser(row),
      },
      {
        label: "Disable",
        variant: "danger" as const,
        onClick: (row: User) => {
          setDisableTarget(row);
          setDisableOpen(true);
        },
      },
    ],
    [openEditUser],
  );

  /* ── Roles columns ──────────────────────────── */
  const roleColumns = useMemo<Column<CustomRole>[]>(
    () => [
      { key: "name", header: "Name", sortKey: "name" },
      {
        key: "permissions",
        header: "Permission Count",
        render: (r) => String(r.permissions.length),
      },
      { key: "created_by", header: "Created By" },
      {
        key: "created_at",
        header: "Created At",
        render: (r) => formatDate(r.created_at),
      },
    ],
    [],
  );

  const roleRowActions = useMemo(
    () => [
      {
        label: "Edit",
        onClick: (row: CustomRole) => openEditRole(row),
      },
      {
        label: "Delete",
        variant: "danger" as const,
        onClick: (row: CustomRole) => {
          setDeleteRoleTarget(row);
          setDeleteRoleOpen(true);
        },
      },
    ],
    [openEditRole],
  );

  /* ── Filter handlers ─────────────────────────── */
  const handleUserFilterChange = useCallback((key: string, value: string) => {
    setUserFilters((prev) => ({ ...prev, [key]: value }));
    setUsersPage(1);
  }, []);

  const handleUserFilterClear = useCallback(() => {
    setUserFilters({ search: "", role: "", status: "" });
    setUsersPage(1);
  }, []);

  /* ── Permission matrix: all role columns ─────── */
  const allRoleColumns = useMemo(() => {
    const cols = FIXED_ROLES.map((r) => ({ id: r.value, label: r.label, permissions: ROLE_PERMISSIONS[r.value] ?? [] }));
    customRoles.forEach((cr) => {
      cols.push({ id: cr.id, label: cr.name, permissions: cr.permissions });
    });
    return cols;
  }, [customRoles]);

  /* ── Render ──────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Users & Roles"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Users & Roles" }]}
        actions={
          activeTab === "users" ? (
            <button className="btn btn-primary" onClick={() => setInviteOpen(true)}>
              Invite User
            </button>
          ) : activeTab === "roles" ? (
            <button className="btn btn-primary" onClick={openCreateRole}>
              Create Role
            </button>
          ) : undefined
        }
      />

      {/* Tabs */}
      <div style={tabBar}>
        <button style={tabBtn(activeTab === "users")} onClick={() => setActiveTab("users")}>
          Users
        </button>
        <button style={tabBtn(activeTab === "roles")} onClick={() => setActiveTab("roles")}>
          Custom Roles
        </button>
        <button style={tabBtn(activeTab === "matrix")} onClick={() => setActiveTab("matrix")}>
          Permission Matrix
        </button>
      </div>

      {/* ── Users Tab ────────────────────────────── */}
      {activeTab === "users" && (
        <>
          <FilterBar
            filters={userFilterDefs}
            values={userFilters}
            onChange={handleUserFilterChange}
            onClear={handleUserFilterClear}
          />

          {!usersLoading && users.length === 0 && !userFilters.search && !userFilters.role && !userFilters.status ? (
            <EmptyState
              title="No users yet"
              description="Invite the first user to this tenant."
              actionLabel="Invite User"
              onAction={() => setInviteOpen(true)}
              icon="&#x263A;"
            />
          ) : (
            <DataTable<User>
              columns={userColumns}
              data={users}
              loading={usersLoading}
              emptyMessage="No users match your filters"
              page={usersPage}
              pageSize={PAGE_SIZE}
              total={usersTotal}
              onPageChange={setUsersPage}
              onRowClick={openUserDetail}
              rowActions={userRowActions}
              striped
            />
          )}
        </>
      )}

      {/* ── Custom Roles Tab ─────────────────────── */}
      {activeTab === "roles" && (
        <>
          {!rolesLoading && customRoles.length === 0 ? (
            <EmptyState
              title="No custom roles"
              description="Create custom roles to fine-tune permissions beyond the 7 fixed roles."
              actionLabel="Create Role"
              onAction={openCreateRole}
              icon="&#x1F511;"
            />
          ) : (
            <DataTable<CustomRole>
              columns={roleColumns}
              data={customRoles}
              loading={rolesLoading}
              emptyMessage="No custom roles"
              rowActions={roleRowActions}
              striped
            />
          )}
        </>
      )}

      {/* ── Permission Matrix Tab ────────────────── */}
      {activeTab === "matrix" && (
        <div className="card" style={{ overflow: "auto", maxHeight: "70vh" }}>
          {rolesLoading ? (
            <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
              <Spinner size="md" />
            </div>
          ) : (
            <table style={matrixTable}>
              <thead>
                <tr>
                  <th style={{ ...matrixTh, textAlign: "left", minWidth: 180 }}>Permission</th>
                  {allRoleColumns.map((rc) => (
                    <th key={rc.id} style={matrixTh}>{rc.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ALL_PERMISSIONS.map((perm, pi) => {
                  const showDomainSep =
                    pi === 0 || ALL_PERMISSIONS[pi - 1].domain !== perm.domain;
                  return (
                    <Fragment key={perm.key}>
                      {showDomainSep && (
                        <tr>
                          <td
                            colSpan={allRoleColumns.length + 1}
                            style={{
                              ...matrixTd,
                              textAlign: "left",
                              fontWeight: 600,
                              fontSize: "0.6875rem",
                              textTransform: "uppercase",
                              letterSpacing: "0.04em",
                              color: "var(--text-secondary)",
                              background: "var(--bg-primary)",
                              borderBottom: "1px solid var(--border-color)",
                              padding: "0.5rem 0.625rem",
                            }}
                          >
                            {perm.domain}
                          </td>
                        </tr>
                      )}
                      <tr>
                        <td style={{ ...matrixTd, textAlign: "left", fontWeight: 500 }}>
                          <code style={{ fontSize: "0.75rem" }}>{perm.key}</code>
                        </td>
                        {allRoleColumns.map((rc) => (
                          <td key={rc.id} style={matrixTd}>
                            {rc.permissions.includes(perm.key) ? (
                              <span style={checkmark}>&#x2713;</span>
                            ) : (
                              <span style={{ color: "var(--text-muted)" }}>--</span>
                            )}
                          </td>
                        ))}
                      </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ── User Detail Panel ────────────────────── */}
      <DetailPanel
        open={detailOpen}
        onClose={() => {
          setDetailOpen(false);
          setSelectedUser(null);
          setUserPermissions([]);
        }}
        title={selectedUser?.name || selectedUser?.email || "User Details"}
        subtitle={selectedUser?.email}
        width={500}
      >
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
            <Spinner size="md" />
          </div>
        ) : selectedUser ? (
          <>
            <div style={sectionTitle}>User Info</div>
            <div style={detailRow}>
              <span style={detailLabel}>Email</span>
              <span style={detailValue}>{selectedUser.email}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Name</span>
              <span style={detailValue}>{selectedUser.name}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Role</span>
              <span style={detailValue}>
                <StatusBadge
                  status={roleLabel(selectedUser.role)}
                  variant={ROLE_VARIANT[selectedUser.role] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Status</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selectedUser.status}
                  variant={USER_STATUS_VARIANT[selectedUser.status] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>MFA Enabled</span>
              <span style={detailValue}>{selectedUser.mfa_enabled ? "Yes" : "No"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Created</span>
              <span style={detailValue}>{formatDate(selectedUser.created_at)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Last Login</span>
              <span style={detailValue}>{formatDateTime(selectedUser.last_login_at)}</span>
            </div>

            <div style={sectionTitle}>Effective Permissions</div>
            {userPermissions.length === 0 ? (
              <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)", padding: "0.5rem 0" }}>
                No permissions computed
              </div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.375rem", marginTop: "0.5rem" }}>
                {userPermissions.map((perm) => (
                  <code
                    key={perm}
                    style={{
                      fontSize: "0.6875rem",
                      padding: "0.125rem 0.5rem",
                      background: "var(--bg-primary)",
                      border: "1px solid var(--border-light)",
                      borderRadius: "999px",
                    }}
                  >
                    {perm}
                  </code>
                ))}
              </div>
            )}

            <div style={{ marginTop: "1.5rem", display: "flex", gap: "0.5rem" }}>
              <button className="btn" onClick={() => { setDetailOpen(false); openEditUser(selectedUser); }}>
                Edit User
              </button>
              {selectedUser.status !== "disabled" && (
                <button
                  className="btn btn-danger"
                  onClick={() => {
                    setDisableTarget(selectedUser);
                    setDisableOpen(true);
                  }}
                >
                  Disable User
                </button>
              )}
            </div>
          </>
        ) : null}
      </DetailPanel>

      {/* ── Invite User Modal ────────────────────── */}
      {inviteOpen && (
        <div style={modalOverlay} onClick={() => setInviteOpen(false)} role="presentation">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="invite-user-title"
            style={modalCard}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="invite-user-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
              Invite User
            </h3>

            <div style={formGroup}>
              <label style={labelStyle}>Email *</label>
              <input
                className="input"
                type="email"
                value={inviteEmail}
                onChange={(e) => {
                  setInviteEmail(e.target.value);
                  setInviteErrors((prev) => ({ ...prev, email: "" }));
                }}
                placeholder="user@example.com"
                style={{ width: "100%" }}
              />
              {inviteErrors.email && <div style={errorText}>{inviteErrors.email}</div>}
            </div>

            <div style={formGroup}>
              <label style={labelStyle}>Role *</label>
              <select
                className="select"
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value)}
                style={{ width: "100%" }}
              >
                {ASSIGNABLE_ROLES.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
              {inviteErrors.role && <div style={errorText}>{inviteErrors.role}</div>}
            </div>

            {customRoles.length > 0 && (
              <div style={formGroup}>
                <label style={labelStyle}>Custom Roles (optional)</label>
                <div style={{
                  border: "1px solid var(--border-color)",
                  borderRadius: "var(--radius-md)",
                  padding: "0.5rem",
                  maxHeight: 140,
                  overflow: "auto",
                }}>
                  {customRoles.map((cr) => (
                    <label key={cr.id} style={{ ...permCheckbox, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={inviteCustomRoles.includes(cr.id)}
                        onChange={() => {
                          setInviteCustomRoles((prev) =>
                            prev.includes(cr.id) ? prev.filter((id) => id !== cr.id) : [...prev, cr.id],
                          );
                        }}
                      />
                      <span>{cr.name}</span>
                      <span style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
                        ({cr.permissions.length} perms)
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
              <button className="btn" onClick={() => setInviteOpen(false)} disabled={inviteLoading}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleInvite} disabled={inviteLoading}>
                {inviteLoading && <Spinner size="sm" />}
                Send Invite
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Edit User Modal ──────────────────────── */}
      {editOpen && selectedUser && (
        <div style={modalOverlay} onClick={() => setEditOpen(false)} role="presentation">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="edit-user-title"
            style={modalCard}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="edit-user-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
              Edit User: {selectedUser.email}
            </h3>

            <div style={formGroup}>
              <label style={labelStyle}>Role</label>
              <select
                className="select"
                value={editRole}
                onChange={(e) => setEditRole(e.target.value)}
                style={{ width: "100%" }}
              >
                {ASSIGNABLE_ROLES.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
            </div>

            <div style={formGroup}>
              <label style={labelStyle}>Status</label>
              <select
                className="select"
                value={editStatus}
                onChange={(e) => setEditStatus(e.target.value)}
                style={{ width: "100%" }}
              >
                <option value="active">Active</option>
                <option value="disabled">Disabled</option>
              </select>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
              <button className="btn" onClick={() => setEditOpen(false)} disabled={editLoading}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleEditUser} disabled={editLoading}>
                {editLoading && <Spinner size="sm" />}
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Disable User Confirm ─────────────────── */}
      <ConfirmDialog
        open={disableOpen}
        title="Disable User"
        message={`Disable user "${disableTarget?.email}"? They will lose access to the console immediately.`}
        confirmLabel="Disable"
        variant="danger"
        onConfirm={handleDisableUser}
        onCancel={() => {
          setDisableOpen(false);
          setDisableTarget(null);
        }}
        loading={disableLoading}
      />

      {/* ── Create/Edit Role Modal ───────────────── */}
      {roleDialogOpen && (
        <div style={modalOverlay} onClick={() => setRoleDialogOpen(false)} role="presentation">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="role-dialog-title"
            style={modalCard}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="role-dialog-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
              {roleDialogMode === "create" ? "Create Custom Role" : "Edit Custom Role"}
            </h3>

            <div style={formGroup}>
              <label style={labelStyle}>Role Name *</label>
              <input
                className="input"
                type="text"
                value={roleName}
                onChange={(e) => {
                  setRoleName(e.target.value);
                  setRoleErrors((prev) => ({ ...prev, name: "" }));
                }}
                placeholder="e.g. network-ops"
                style={{ width: "100%" }}
              />
              {roleErrors.name && <div style={errorText}>{roleErrors.name}</div>}
            </div>

            <div style={formGroup}>
              <label style={labelStyle}>Permissions *</label>
              {roleErrors.permissions && <div style={errorText}>{roleErrors.permissions}</div>}
              <div style={{
                border: "1px solid var(--border-color)",
                borderRadius: "var(--radius-md)",
                padding: "0.75rem",
                maxHeight: 320,
                overflow: "auto",
              }}>
                {PERMISSION_DOMAINS.map((domain) => (
                  <div key={domain}>
                    <div style={domainHeader}>{domain}</div>
                    {ALL_PERMISSIONS.filter((p) => p.domain === domain).map((perm) => (
                      <label key={perm.key} style={{ ...permCheckbox, cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={rolePermissions.includes(perm.key)}
                          onChange={() => togglePermission(perm.key)}
                        />
                        <code style={{ fontSize: "0.75rem" }}>{perm.key}</code>
                        <span style={{ color: "var(--text-muted)", fontSize: "0.75rem" }}>
                          ({perm.label})
                        </span>
                      </label>
                    ))}
                  </div>
                ))}
              </div>
              <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.375rem" }}>
                {rolePermissions.length} permission{rolePermissions.length !== 1 ? "s" : ""} selected
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
              <button className="btn" onClick={() => setRoleDialogOpen(false)} disabled={roleDialogLoading}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleSaveRole} disabled={roleDialogLoading}>
                {roleDialogLoading && <Spinner size="sm" />}
                {roleDialogMode === "create" ? "Create Role" : "Save Changes"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Delete Role Confirm ──────────────────── */}
      <ConfirmDialog
        open={deleteRoleOpen}
        title="Delete Custom Role"
        message={`Delete role "${deleteRoleTarget?.name}"? Users assigned this role will lose its permissions.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleDeleteRole}
        onCancel={() => {
          setDeleteRoleOpen(false);
          setDeleteRoleTarget(null);
        }}
        loading={deleteRoleLoading}
      />
    </div>
  );
}
