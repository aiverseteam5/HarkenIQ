"""CCConfig loading and validation."""

from harkeniq_cc.config import CCConfig, _coerce, load_cc_config


class TestDefaults:
    def test_defaults(self):
        config = load_cc_config(env={})
        assert config.tenant_id == ""
        assert config.http_port == 8090
        assert config.insecure is False
        assert config.dsn == "sqlite+aiosqlite:///:memory:"
        assert config.keycloak_client_id == "harkeniq-cc"
        assert config.usage_report_interval_s == 86400.0
        assert config.site_poll_interval_s == 300.0


class TestEnvOverrides:
    def test_env_overrides(self):
        config = load_cc_config(env={
            "HARKEN_CC_TENANT_ID": "tenant-abc",
            "HARKEN_CC_HTTP_PORT": "9090",
            "HARKEN_CC_INSECURE": "true",
            "HARKEN_CC_CONSOLE_URL": "https://console.harkeniq.com",
            "HARKEN_CC_SITE_POLL_INTERVAL_S": "60.0",
        })
        assert config.tenant_id == "tenant-abc"
        assert config.http_port == 9090
        assert config.insecure is True
        assert config.console_url == "https://console.harkeniq.com"
        assert config.site_poll_interval_s == 60.0


class TestYaml:
    def test_yaml_then_env(self, tmp_path):
        path = tmp_path / "cc.yaml"
        path.write_text(
            "tenant_id: tenant-yaml\nhttp_port: 9000\n"
            "site_poll_interval_s: 120\n"
        )
        config = load_cc_config(
            env={"HARKEN_CC_HTTP_PORT": "9001"}, yaml_path=str(path)
        )
        assert config.tenant_id == "tenant-yaml"
        assert config.http_port == 9001  # env wins over yaml
        assert config.site_poll_interval_s == 120


class TestBoolCoerce:
    def test_true_values(self):
        assert _coerce("true", False) is True
        assert _coerce("1", False) is True
        assert _coerce("yes", False) is True
        assert _coerce("on", False) is True

    def test_false_values(self):
        assert _coerce("false", True) is False
        assert _coerce("0", True) is False
        assert _coerce("no", True) is False
        assert _coerce("off", True) is False


class TestValidation:
    def test_validate_missing_tenant_id(self):
        config = CCConfig()
        errors = config.validate()
        assert any("tenant_id" in e for e in errors)

    def test_validate_insecure_ok(self):
        config = CCConfig(insecure=True)
        assert config.validate() == []

    def test_validate_invalid_port(self):
        config = CCConfig(insecure=True, http_port=0)
        errors = config.validate()
        assert any("http_port" in e for e in errors)


class TestEnvPrefix:
    def test_all_env_vars_start_with_harken_cc(self):
        from harkeniq_cc.config import _ENV_MAP
        for key in _ENV_MAP:
            assert key.startswith("HARKEN_CC_"), f"{key} does not start with HARKEN_CC_"
