"""Mock Redfish simulator: serves fixture JSON over HTTPS with fault injection.

Usage:
    Programmatic (tests):
        sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
        await sim.start()
        url = sim.url  # e.g. "https://localhost:18443"
        ...
        await sim.stop()

    CLI:
        harken mock start --device dell-r750 --port 8443

See Doc 11 for full specification.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import ssl
import tempfile
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger("harkeniq.mock")

FIXTURES_DIR = Path(__file__).parent / "fixtures"

DEVICE_PROFILES = {
    "dell-r750": {"vendor": "dell", "model": "PowerEdge R750", "controller": "iDRAC9"},
    "dell-r760": {"vendor": "dell", "model": "PowerEdge R760", "controller": "iDRAC10"},
    "hpe-dl360-gen10": {"vendor": "hpe", "model": "ProLiant DL360 Gen10", "controller": "iLO 5"},
    "hpe-dl380-gen11": {"vendor": "hpe", "model": "ProLiant DL380 Gen11", "controller": "iLO 6"},
}


# R4-2 P13: default BMC config attributes (iDRAC-style keys) served from
# the DellAttributes endpoint; mutable so tests can inject config drift.
_DEFAULT_DELL_ATTRIBUTES = {
    "NTPConfigGroup.1.NTPEnable": "Enabled",
    "NTPConfigGroup.1.NTP1": "pool.ntp.org",
    "SysLog.1.SysLogEnable": "Enabled",
    "WebServer.1.HttpsRedirection": "Enabled",
    "ThermalSettings.1.FanSpeedOffset": "Off",
}


class MockSimulator:
    """Mock Redfish BMC simulator with fault injection."""

    def __init__(
        self,
        device: str = "dell-r750",
        port: int = 0,
        host: str = "127.0.0.1",
        no_auth: bool = False,
        credentials: str = "admin:password",
    ):
        if device not in DEVICE_PROFILES:
            raise ValueError(f"Unknown device profile: {device}. Valid: {list(DEVICE_PROFILES)}")
        self.device = device
        self.profile = DEVICE_PROFILES[device]
        self.requested_port = port
        self.host = host
        self.no_auth = no_auth
        self.username, self.password = credentials.split(":", 1)

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: int = 0
        self._state: dict[str, Any] = {}
        self._action_state: dict[str, Any] = {"led": {}, "diagnostics": [], "fan_reset": []}
        self._bmc_attributes: dict[str, Any] = dict(_DEFAULT_DELL_ATTRIBUTES)
        self._sessions: dict[str, dict] = {}
        self._gradual_tasks: list = []
        self._fixtures_path = FIXTURES_DIR / device.replace("-", "_")

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    @property
    def action_state(self) -> dict[str, Any]:
        """Applied action state (LED, diagnostics, fan reset) for assertions."""
        return self._action_state

    @property
    def bmc_attributes(self) -> dict[str, Any]:
        """Current BMC config attributes (R4-2 P13)."""
        return dict(self._bmc_attributes)

    def inject_config_drift(self, key: str, value: Any) -> None:
        """Drift one BMC config attribute away from its default (R4-2 P13)."""
        self._bmc_attributes[key] = value

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the mock HTTPS server."""
        self._load_fixtures()
        self._app = web.Application()
        self._register_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        ssl_ctx = self._create_ssl_context()
        self._site = web.TCPSite(
            self._runner,
            self.host,
            self.requested_port,
            ssl_context=ssl_ctx,
        )
        await self._site.start()

        # Resolve actual port (important when requested_port=0)
        for sock in self._site._server.sockets:
            self._port = sock.getsockname()[1]
            break

        logger.info("Mock simulator started: %s on port %d", self.device, self._port)

    async def stop(self):
        """Stop the mock server and cancel gradual fault tasks."""
        for task in self._gradual_tasks:
            task.cancel()
        self._gradual_tasks.clear()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Mock simulator stopped: %s", self.device)

    # ------------------------------------------------------------------
    # Fixture loading
    # ------------------------------------------------------------------

    def _load_fixtures(self):
        """Load all JSON fixture files into mutable in-memory state."""
        self._state = {}
        self._action_state = {"led": {}, "diagnostics": [], "fan_reset": []}
        self._bmc_attributes = dict(_DEFAULT_DELL_ATTRIBUTES)
        if not self._fixtures_path.exists():
            logger.warning("No fixtures directory: %s", self._fixtures_path)
            return
        for f in self._fixtures_path.glob("*.json"):
            with open(f) as fh:
                self._state[f.stem] = json.load(fh)
        self._generate_dimm_fixtures()
        logger.info("Loaded %d fixtures from %s", len(self._state), self._fixtures_path)

    def _generate_dimm_fixtures(self):
        """Expand the single DIMM template into all 16 DIMMs.

        Doc 11 §5: "A single fixture template generates all 16 with
        slot-specific IDs and serial numbers." Also rebuilds
        memory_collection to list all 16 members.
        """
        vendor = self.profile["vendor"]
        if vendor == "dell":
            template_key = "memory_dimm_a1"
            metrics_template_key = "memory_metrics_a1"
            base_uri = "/redfish/v1/Systems/System.Embedded.1/Memory"
            # (state_suffix, dimm_id, display_name, socket, slot, bank)
            slots = [
                (f"{b.lower()}{s}", f"DIMM.Socket.{b}{s}", f"DIMM {b}{s}", 1 if b == "A" else 2, s, b)
                for b in ("A", "B") for s in range(1, 9)
            ]
        else:
            template_key = "memory_dimm_proc1dimm1"
            metrics_template_key = "memory_metrics_proc1dimm1"
            base_uri = "/redfish/v1/Systems/1/Memory"
            slots = [
                (f"proc{p}dimm{d}", f"proc{p}dimm{d}", f"proc{p}dimm{d}", p, d, "")
                for p in (1, 2) for d in range(1, 9)
            ]

        dimm_template = self._state.get(template_key)
        metrics_template = self._state.get(metrics_template_key)
        if dimm_template is None or metrics_template is None:
            return

        base_serial = dimm_template.get("SerialNumber", "00000000")
        members = []
        for idx, (suffix, dimm_id, name, socket, slot, bank) in enumerate(slots):
            dimm_uri = f"{base_uri}/{dimm_id}"
            members.append({"@odata.id": dimm_uri})

            dimm_key = f"memory_dimm_{suffix}"
            if dimm_key not in self._state:
                dimm = copy.deepcopy(dimm_template)
                dimm["@odata.id"] = dimm_uri
                dimm["Id"] = dimm_id
                dimm["Name"] = name
                dimm["SerialNumber"] = f"{base_serial}{idx:02d}"
                if "Description" in dimm:
                    dimm["Description"] = f"DIMM {name}"
                if "DeviceLocator" in dimm:
                    dimm["DeviceLocator"] = f"PROC {socket} DIMM {slot}"
                loc = dimm.get("MemoryLocation")
                if isinstance(loc, dict):
                    loc["Socket"] = socket
                    loc["Slot"] = slot
                if bank:
                    dell_mem = dimm.get("Oem", {}).get("Dell", {}).get("DellMemory")
                    if isinstance(dell_mem, dict):
                        dell_mem["BankLabel"] = bank
                self._state[dimm_key] = dimm

            metrics_key = f"memory_metrics_{suffix}"
            if metrics_key not in self._state:
                metrics = copy.deepcopy(metrics_template)
                metrics["@odata.id"] = f"{dimm_uri}/MemoryMetrics"
                if "Name" in metrics:
                    metrics["Name"] = f"Memory Metrics for {name}"
                self._state[metrics_key] = metrics

        collection = self._state.get("memory_collection", {})
        collection["Members"] = members
        collection["Members@odata.count"] = len(members)
        self._state["memory_collection"] = collection

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(self):
        """Register Redfish and test control routes."""
        r = self._app.router

        # Redfish service root
        r.add_get("/redfish/v1/", self._handle_fixture("service_root"))
        r.add_get("/redfish/v1", self._handle_fixture("service_root"))

        vendor = self.profile["vendor"]
        if vendor == "dell":
            self._register_dell_routes(r)
        else:
            self._register_hpe_routes(r)

        # Session management
        r.add_post("/redfish/v1/SessionService/Sessions", self._handle_create_session)
        r.add_delete("/redfish/v1/SessionService/Sessions/{session_id}", self._handle_delete_session)

        # Test control endpoints
        r.add_post("/test/inject-fault", self._handle_inject_fault)
        r.add_post("/test/inject-gradual", self._handle_inject_gradual)
        r.add_post("/test/inject-log", self._handle_inject_log)
        r.add_post("/test/reset", self._handle_reset)
        r.add_get("/test/state", self._handle_get_state)
        r.add_get("/test/action-state", self._handle_action_state)
        r.add_post("/test/config", self._handle_config)

    def _register_dell_routes(self, r):
        """Register Dell iDRAC Redfish routes."""
        sid = "System.Embedded.1"
        mid = "iDRAC.Embedded.1"

        r.add_get(f"/redfish/v1/Managers/{mid}", self._handle_fixture("manager"))
        r.add_get(f"/redfish/v1/Systems/{sid}", self._handle_fixture("system"))
        r.add_get(f"/redfish/v1/Chassis/{sid}/Thermal", self._handle_fixture("thermal"))
        r.add_get(f"/redfish/v1/Chassis/{sid}/Power", self._handle_fixture("power"))
        r.add_get(f"/redfish/v1/Systems/{sid}/Storage", self._handle_fixture("storage_collection"))
        r.add_get(f"/redfish/v1/Systems/{sid}/Storage/RAID.Slot.1-1", self._handle_fixture("storage_controller_raid"))
        r.add_get(f"/redfish/v1/Systems/{sid}/Memory", self._handle_fixture("memory_collection"))
        r.add_get(f"/redfish/v1/Managers/{mid}/LogServices/Sel/Entries", self._handle_fixture("sel_entries"))

        # Individual drives
        for i in range(4):
            drive_id = f"Disk.Bay.{i}:Enclosure.Internal.0-1:RAID.Slot.1-1"
            r.add_get(
                f"/redfish/v1/Systems/{sid}/Storage/RAID.Slot.1-1/Drives/{drive_id}",
                self._handle_fixture(f"drive_{i}"),
            )

        # Action endpoints (Doc 06 §11A.4)
        r.add_get(f"/redfish/v1/Chassis/{sid}/Drives/{{drive_id}}", self._handle_chassis_drive_get)
        r.add_patch(f"/redfish/v1/Chassis/{sid}/Drives/{{drive_id}}", self._handle_chassis_drive_patch)
        r.add_post(
            f"/redfish/v1/Managers/{mid}/Oem/Dell/DellLCService"
            "/Actions/DellLCService.ExportSystemConfiguration",
            self._handle_dell_export_sysconfig,
        )
        r.add_patch(
            f"/redfish/v1/Managers/{mid}/Oem/Dell/DellAttributes/{mid}",
            self._handle_dell_attributes_patch,
        )
        # R4-2 P13: config attribute surface for drift detection
        r.add_get(
            f"/redfish/v1/Managers/{mid}/Oem/Dell/DellAttributes/{mid}",
            self._handle_dell_attributes_get,
        )

        # Individual DIMMs and MemoryMetrics
        for bank in ("A", "B"):
            for slot in range(1, 9):
                dimm_id = f"DIMM.Socket.{bank}{slot}"
                r.add_get(
                    f"/redfish/v1/Systems/{sid}/Memory/{dimm_id}",
                    self._handle_fixture(f"memory_dimm_{bank.lower()}{slot}"),
                )
                r.add_get(
                    f"/redfish/v1/Systems/{sid}/Memory/{dimm_id}/MemoryMetrics",
                    self._handle_fixture(f"memory_metrics_{bank.lower()}{slot}"),
                )

    def _register_hpe_routes(self, r):
        """Register HPE iLO Redfish routes."""
        r.add_get("/redfish/v1/Managers/1", self._handle_fixture("manager"))
        r.add_get("/redfish/v1/Systems/1", self._handle_fixture("system"))
        r.add_get("/redfish/v1/Chassis/1/Thermal", self._handle_fixture("thermal"))
        r.add_get("/redfish/v1/Chassis/1/Power", self._handle_fixture("power"))
        r.add_get("/redfish/v1/Systems/1/Storage", self._handle_fixture("storage_collection"))
        r.add_get("/redfish/v1/Systems/1/Storage/DE009000", self._handle_fixture("storage_controller_de009000"))
        r.add_get("/redfish/v1/Systems/1/Memory", self._handle_fixture("memory_collection"))
        r.add_get("/redfish/v1/Systems/1/LogServices/IML/Entries", self._handle_fixture("iml_entries"))

        # Individual drives (HPE uses simpler IDs)
        for i in range(4):
            r.add_get(
                f"/redfish/v1/Systems/1/Storage/DE009000/Drives/{i}",
                self._handle_fixture(f"drive_{i}"),
            )

        # Action endpoints (Doc 06 §11A.4)
        r.add_get("/redfish/v1/Chassis/1/Drives/{drive_id}", self._handle_chassis_drive_get)
        r.add_patch("/redfish/v1/Chassis/1/Drives/{drive_id}", self._handle_chassis_drive_patch)
        r.add_get("/redfish/v1/Managers/1/ActiveHealthSystem", self._handle_hpe_ahs)
        r.add_patch("/redfish/v1/Chassis/1/Thermal", self._handle_hpe_thermal_patch)

        # Individual DIMMs and MemoryMetrics (HPE: proc{n}dimm{m})
        for proc in (1, 2):
            for dimm in range(1, 9):
                dimm_id = f"proc{proc}dimm{dimm}"
                r.add_get(
                    f"/redfish/v1/Systems/1/Memory/{dimm_id}",
                    self._handle_fixture(f"memory_dimm_{dimm_id}"),
                )
                r.add_get(
                    f"/redfish/v1/Systems/1/Memory/{dimm_id}/MemoryMetrics",
                    self._handle_fixture(f"memory_metrics_{dimm_id}"),
                )

        # SmartStorage (iLO5 only)
        if "gen10" in self.device:
            r.add_get(
                "/redfish/v1/Systems/1/SmartStorage/ArrayControllers",
                self._handle_fixture("smartstorage_controllers"),
            )
            r.add_get(
                "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0/DiskDrives",
                self._handle_fixture("smartstorage_drives"),
            )

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def _handle_fixture(self, fixture_name: str):
        """Create a handler that returns fixture data from state."""
        async def handler(request: web.Request) -> web.Response:
            if not self._check_auth(request):
                return web.json_response(
                    {"error": {"message": "No valid session", "code": "Base.1.0.NoValidSession"}},
                    status=401,
                )
            data = self._state.get(fixture_name)
            if data is None:
                return web.json_response(
                    {"error": {"message": f"Resource not found: {fixture_name}"}},
                    status=404,
                )
            return web.json_response(data)
        return handler

    def _check_auth(self, request: web.Request) -> bool:
        """Validate session token. Returns True if auth passes."""
        if self.no_auth:
            return True
        token = request.headers.get("X-Auth-Token", "")
        return token in self._sessions

    async def _handle_create_session(self, request: web.Request) -> web.Response:
        """Create a new Redfish session. Returns X-Auth-Token."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)

        username = body.get("UserName", "")
        password = body.get("Password", "")
        if username != self.username or password != self.password:
            return web.json_response(
                {"error": {"message": "Invalid credentials", "code": "Base.1.0.InvalidCredentials"}},
                status=401,
            )

        token = secrets.token_hex(16)
        session_id = secrets.token_hex(8)
        self._sessions[token] = {"id": session_id, "username": username}

        return web.json_response(
            {"@odata.id": f"/redfish/v1/SessionService/Sessions/{session_id}", "Id": session_id},
            status=201,
            headers={"X-Auth-Token": token, "Location": f"/redfish/v1/SessionService/Sessions/{session_id}"},
        )

    async def _handle_delete_session(self, request: web.Request) -> web.Response:
        """Delete a Redfish session."""
        session_id = request.match_info["session_id"]
        to_remove = [k for k, v in self._sessions.items() if v["id"] == session_id]
        for k in to_remove:
            del self._sessions[k]
        return web.Response(status=200)

    # ------------------------------------------------------------------
    # Action endpoints (Doc 06 §11A.4)
    # ------------------------------------------------------------------

    def _find_drive(self, drive_id: str) -> Optional[dict]:
        """Find a drive fixture by Redfish Id, Name, or Model.

        Model is included because Dell normalization uses it as the
        human-readable drive name, and skill action targets carry the
        normalized name.
        """
        for key, data in self._state.items():
            if not key.startswith("drive_"):
                continue
            if drive_id in (data.get("Id"), data.get("Name"), data.get("Model")):
                return data
        return None

    async def _handle_chassis_drive_get(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        drive = self._find_drive(request.match_info["drive_id"])
        if drive is None:
            return web.json_response({"error": {"message": "Drive not found"}}, status=404)
        return web.json_response(drive)

    async def _handle_chassis_drive_patch(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        drive = self._find_drive(request.match_info["drive_id"])
        if drive is None:
            return web.json_response({"error": {"message": "Drive not found"}}, status=404)
        body = await request.json()
        if "IndicatorLED" in body:
            drive["IndicatorLED"] = body["IndicatorLED"]
            self._action_state["led"][drive.get("Name", drive.get("Id", ""))] = body["IndicatorLED"]
        return web.json_response(drive)

    async def _handle_dell_export_sysconfig(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        body = await request.json()
        self._action_state["diagnostics"].append({"vendor": "dell", "params": body})
        return web.json_response(
            {"Id": "JID_000000000001", "TaskState": "Completed"}, status=202
        )

    async def _handle_dell_attributes_patch(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        body = await request.json()
        attributes = body.get("Attributes", {})
        self._action_state["fan_reset"].append({"vendor": "dell", "attributes": attributes})
        # R4-2 P13: attribute writes persist, so CONFIG_RESTORE is verifiable
        self._bmc_attributes.update(attributes)
        return web.json_response({"Attributes": attributes})

    async def _handle_dell_attributes_get(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        return web.json_response({"Attributes": dict(self._bmc_attributes)})

    async def _handle_hpe_ahs(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        self._action_state["diagnostics"].append(
            {"vendor": "hpe", "resource": "ActiveHealthSystem"}
        )
        return web.json_response(
            {"@odata.id": "/redfish/v1/Managers/1/ActiveHealthSystem",
             "Name": "Active Health System"}
        )

    async def _handle_hpe_thermal_patch(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "No valid session"}}, status=401)
        body = await request.json()
        self._action_state["fan_reset"].append({"vendor": "hpe", "body": body})
        return web.json_response(self._state.get("thermal", {}))

    async def _handle_action_state(self, request: web.Request) -> web.Response:
        """Return applied action state for test assertions."""
        return web.json_response(self._action_state)

    # ------------------------------------------------------------------
    # Fault injection (Doc 11 §7)
    # ------------------------------------------------------------------

    async def _handle_inject_fault(self, request: web.Request) -> web.Response:
        """Inject a fault into the simulated device state."""
        body = await request.json()
        fault_type = body.get("fault_type", "")
        target = body.get("target", "")
        params = body.get("params", {})

        try:
            self._apply_fault(fault_type, target, params)
        except (KeyError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)

        return web.json_response({"status": "injected", "fault_type": fault_type, "target": target})

    async def _handle_inject_gradual(self, request: web.Request) -> web.Response:
        """Inject a gradually changing value (for trending demos)."""
        import asyncio

        body = await request.json()
        fault_type = body.get("fault_type", "")
        target = body.get("target", "")
        params = body.get("params", {})
        field = params.get("field", "")
        start_value = params.get("start_value", 0)
        rate_per_second = params.get("rate_per_second", 0)
        duration_seconds = params.get("duration_seconds", 60)

        async def _gradual_task():
            current = start_value
            elapsed = 0
            while elapsed < duration_seconds:
                self._apply_gradual_value(fault_type, target, field, current)
                await asyncio.sleep(1)
                elapsed += 1
                current += rate_per_second

        task = asyncio.create_task(_gradual_task())
        self._gradual_tasks.append(task)
        return web.json_response({"status": "gradual_started", "target": target, "field": field})

    async def _handle_inject_log(self, request: web.Request) -> web.Response:
        """Add a log entry to the simulated device."""
        body = await request.json()
        log_key = "sel_entries" if self.profile["vendor"] == "dell" else "iml_entries"
        log_data = self._state.get(log_key, {})
        members = log_data.get("Members", [])
        new_entry = {
            "Id": str(len(members) + 1),
            "Severity": body.get("severity", "Warning"),
            "Message": body.get("message", ""),
            "MessageId": body.get("message_id", ""),
            "Created": body.get("timestamp", "2026-09-15T14:30:00Z"),
        }
        members.append(new_entry)
        log_data["Members"] = members
        log_data["Members@odata.count"] = len(members)
        self._state[log_key] = log_data
        return web.json_response({"status": "log_added"})

    async def _handle_reset(self, request: web.Request) -> web.Response:
        """Reset device state to healthy fixtures."""
        for task in self._gradual_tasks:
            task.cancel()
        self._gradual_tasks.clear()
        self._load_fixtures()
        return web.json_response({"status": "reset"})

    async def _handle_get_state(self, request: web.Request) -> web.Response:
        """Return current device state for test assertions."""
        return web.json_response(self._state)

    async def _handle_config(self, request: web.Request) -> web.Response:
        """Configure error simulation (latency, error rate)."""
        body = await request.json()
        # Store config for middleware to use
        self._app["sim_config"] = body
        return web.json_response({"status": "configured", "config": body})

    # ------------------------------------------------------------------
    # Fault application logic
    # ------------------------------------------------------------------

    def _apply_fault(self, fault_type: str, target: str, params: dict):
        """Mutate in-memory state to simulate a fault."""
        if fault_type == "fan":
            self._apply_fan_fault(target, params)
        elif fault_type == "disk":
            self._apply_disk_fault(target, params)
        elif fault_type == "memory":
            self._apply_memory_fault(target, params)
        elif fault_type == "psu":
            self._apply_psu_fault(target, params)
        elif fault_type == "thermal":
            self._apply_thermal_fault(target, params)
        else:
            raise ValueError(f"Unknown fault type: {fault_type}")

    def _apply_fan_fault(self, target: str, params: dict):
        thermal = self._state.get("thermal", {})
        for fan in thermal.get("Fans", []):
            fan_name = fan.get("FanName", fan.get("Name", ""))
            if target in fan_name:
                if "health" in params:
                    fan.setdefault("Status", {})["Health"] = params["health"]
                if "state" in params:
                    fan.setdefault("Status", {})["State"] = params["state"]
                if "speed_rpm" in params:
                    fan["Reading"] = params["speed_rpm"]
                break
        if "redundancy_health" in params:
            for red in thermal.get("Redundancy", []):
                red.setdefault("Status", {})["Health"] = params["redundancy_health"]

    def _apply_disk_fault(self, target: str, params: dict):
        for key, data in self._state.items():
            if not key.startswith("drive_"):
                continue
            drive_name = data.get("Name", data.get("Id", ""))
            if target in drive_name or target in key:
                if "health" in params:
                    data.setdefault("Status", {})["Health"] = params["health"]
                if "life_left_pct" in params:
                    data["PredictedMediaLifeLeftPercent"] = params["life_left_pct"]
                if "smart_alert" in params:
                    data["FailurePredicted"] = params["smart_alert"]
                break

    def _apply_memory_fault(self, target: str, params: dict):
        for key, data in self._state.items():
            if not key.startswith("memory_metrics_"):
                continue
            if target.lower() in key.lower():
                hd = data.setdefault("HealthData", {})
                at = hd.setdefault("AlarmTrips", {})
                if "alarm_ecc_correctable" in params:
                    at["CorrectableECCError"] = params["alarm_ecc_correctable"]
                if "alarm_ecc_uncorrectable" in params:
                    at["UncorrectableECCError"] = params["alarm_ecc_uncorrectable"]
                if "ecc_correctable_lifetime" in params:
                    lt = data.setdefault("LifeTime", {})
                    lt["CorrectableECCErrorCount"] = params["ecc_correctable_lifetime"]
                break
        # Also update the DIMM health status
        for key, data in self._state.items():
            if not key.startswith("memory_dimm_"):
                continue
            if target.lower() in key.lower():
                if "health" in params:
                    data.setdefault("Status", {})["Health"] = params["health"]
                break

    def _apply_psu_fault(self, target: str, params: dict):
        power = self._state.get("power", {})
        for psu in power.get("PowerSupplies", []):
            psu_name = psu.get("Name", psu.get("MemberId", ""))
            if target in psu_name or target == f"PS{int(psu.get('MemberId', -1)) + 1}":
                if "health" in params:
                    psu.setdefault("Status", {})["Health"] = params["health"]
                if "state" in params:
                    psu.setdefault("Status", {})["State"] = params["state"]
                if "output_watts" in params:
                    psu["LastPowerOutputWatts"] = params["output_watts"]
                if "input_voltage" in params:
                    psu["LineInputVoltage"] = params["input_voltage"]
                break
        if "redundancy_health" in params:
            for red in power.get("Redundancy", []):
                red.setdefault("Status", {})["Health"] = params["redundancy_health"]

    def _apply_thermal_fault(self, target: str, params: dict):
        thermal = self._state.get("thermal", {})
        for temp in thermal.get("Temperatures", []):
            temp_name = temp.get("Name", "")
            if target.lower() in temp_name.lower():
                if "reading_c" in params:
                    temp["ReadingCelsius"] = params["reading_c"]
                if "health" in params:
                    temp.setdefault("Status", {})["Health"] = params["health"]
                break

    def _apply_gradual_value(self, fault_type: str, target: str, field: str, value: float):
        """Set a single field value for gradual fault injection."""
        if fault_type == "fan":
            thermal = self._state.get("thermal", {})
            for fan in thermal.get("Fans", []):
                fan_name = fan.get("FanName", fan.get("Name", ""))
                if target in fan_name:
                    if field == "speed_rpm":
                        fan["Reading"] = int(value)
                    break
        elif fault_type == "thermal":
            thermal = self._state.get("thermal", {})
            for temp in thermal.get("Temperatures", []):
                if target.lower() in temp.get("Name", "").lower():
                    if field == "reading_c":
                        temp["ReadingCelsius"] = round(value, 1)
                    break

    # ------------------------------------------------------------------
    # TLS
    # ------------------------------------------------------------------

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context with self-signed certificate."""
        cert_dir = Path(tempfile.gettempdir()) / "harkeniq-mock"
        cert_dir.mkdir(exist_ok=True)
        cert_path = cert_dir / "mock_cert.pem"
        key_path = cert_dir / "mock_key.pem"

        if not cert_path.exists() or not key_path.exists():
            self._generate_self_signed_cert(cert_path, key_path)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        return ctx

    @staticmethod
    def _generate_self_signed_cert(cert_path: Path, key_path: Path):
        """Generate a self-signed certificate for the mock server."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "harkeniq-mock")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    # ------------------------------------------------------------------
    # Programmatic fault injection API (for tests)
    # ------------------------------------------------------------------

    async def inject_fault(self, fault_type: str, target: str, params: dict):
        """Inject a fault programmatically (no HTTP call needed)."""
        self._apply_fault(fault_type, target, params)

    async def reset(self):
        """Reset state to healthy fixtures."""
        for task in self._gradual_tasks:
            task.cancel()
        self._gradual_tasks.clear()
        self._load_fixtures()
