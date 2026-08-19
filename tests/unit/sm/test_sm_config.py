"""SMConfig loading and validation."""

from harkeniq_sm.config import SMConfig, load_sm_config


class TestDefaults:
    def test_defaults(self):
        config = load_sm_config(env={})
        assert config.site_name == "site-1"
        assert config.grpc_port == 50051
        assert config.http_port == 8080
        assert config.insecure is False
        assert config.correlation.shared_power_min_devices == 2
        assert config.correlation.shared_power_window_s == 120.0


class TestEnvOverrides:
    def test_env_overrides(self):
        config = load_sm_config(env={
            "HARKEN_SM_SITE_NAME": "dc-blr-1",
            "HARKEN_SM_GRPC_PORT": "50099",
            "HARKEN_SM_SITE_TOKEN": "secret",
            "HARKEN_SM_INSECURE": "true",
            "HARKEN_SM_CORR_SHARED_POWER_WINDOW_S": "5.0",
        })
        assert config.site_name == "dc-blr-1"
        assert config.grpc_port == 50099
        assert config.site_token == "secret"
        assert config.insecure is True
        assert config.correlation.shared_power_window_s == 5.0


class TestYaml:
    def test_yaml_then_env(self, tmp_path):
        path = tmp_path / "sm.yaml"
        path.write_text(
            "site_name: dc-yaml\nhttp_port: 9000\n"
            "correlation:\n  rack_thermal_window_s: 60\n"
        )
        config = load_sm_config(
            env={"HARKEN_SM_HTTP_PORT": "9001"}, yaml_path=str(path)
        )
        assert config.site_name == "dc-yaml"
        assert config.http_port == 9001  # env wins over yaml
        assert config.correlation.rack_thermal_window_s == 60


class TestValidation:
    def test_secure_requires_token_and_certs(self):
        config = SMConfig()
        errors = config.validate()
        assert any("site_token" in e for e in errors)
        assert any("tls_cert" in e for e in errors)

    def test_insecure_lab_ok(self):
        config = SMConfig(insecure=True)
        assert config.validate() == []

    def test_secure_complete_ok(self):
        config = SMConfig(
            site_token="s", tls_cert="/certs/server.pem", tls_key="/certs/key.pem"
        )
        assert config.validate() == []

    def test_stale_before_unobserved(self):
        config = SMConfig(insecure=True, stale_after_s=700.0, unobserved_after_s=600.0)
        assert any("stale_after_s" in e for e in config.validate())
