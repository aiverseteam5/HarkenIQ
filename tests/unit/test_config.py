"""Config loading tests (Doc 06 §5)."""

import pytest
from click.testing import CliRunner

from harkeniq.cli import main as cli_main
from harkeniq.config import (
    DEFAULTS,
    load_config,
    load_config_file,
    render_config,
    validate_config,
)
from harkeniq.errors import ConfigError


def write_yaml(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


class TestPrecedence:
    def test_defaults_only(self):
        config = load_config(env={})
        assert config["polling"]["sensor_interval"] == 60
        assert config["heartbeat"]["port"] == 5150
        assert config["debounce"]["critical"] == [2, 3]
        # Returned config is a copy, not DEFAULTS itself
        config["polling"]["sensor_interval"] = 1
        assert DEFAULTS["polling"]["sensor_interval"] == 60

    def test_file_overrides_defaults(self, tmp_path):
        path = write_yaml(tmp_path, "bmc:\n  host: 10.0.0.5\npolling:\n  sensor_interval: 30\n")
        config = load_config(path, env={})
        assert config["bmc"]["host"] == "10.0.0.5"
        assert config["polling"]["sensor_interval"] == 30
        # untouched siblings keep defaults
        assert config["bmc"]["port"] == 443
        assert config["polling"]["log_interval"] == 300

    def test_env_overrides_file(self, tmp_path):
        path = write_yaml(tmp_path, "bmc:\n  host: from-file\n")
        config = load_config(path, env={"HARKENIQ_BMC_HOST": "from-env"})
        assert config["bmc"]["host"] == "from-env"

    def test_cli_overrides_env(self, tmp_path):
        path = write_yaml(tmp_path, "bmc:\n  host: from-file\n")
        config = load_config(
            path,
            cli_overrides={"bmc": {"host": "from-cli"}},
            env={"HARKENIQ_BMC_HOST": "from-env"},
        )
        assert config["bmc"]["host"] == "from-cli"

    def test_peers_list_replaced_not_merged(self, tmp_path):
        path = write_yaml(tmp_path, "peers:\n  - host: 10.0.1.101\n    port: 5150\n")
        config = load_config(path, env={})
        assert config["peers"] == [{"host": "10.0.1.101", "port": 5150}]


class TestEnvCoercion:
    def test_int_coercion(self):
        config = load_config(env={"HARKENIQ_HEARTBEAT_PORT": "6000"})
        assert config["heartbeat"]["port"] == 6000

    def test_bool_coercion(self):
        config = load_config(env={"HARKENIQ_BMC_VERIFY_SSL": "true"})
        assert config["bmc"]["verify_ssl"] is True
        config = load_config(env={"HARKENIQ_BMC_VERIFY_SSL": "0"})
        assert config["bmc"]["verify_ssl"] is False

    def test_float_coercion(self):
        config = load_config(env={"HARKENIQ_TRENDING_SLOPE_THRESHOLD": "0.1"})
        assert config["trending"]["slope_threshold"] == 0.1

    def test_bad_int_raises(self):
        with pytest.raises(ConfigError, match="HARKENIQ_HEARTBEAT_PORT"):
            load_config(env={"HARKENIQ_HEARTBEAT_PORT": "not-a-port"})


class TestFileLoading:
    def test_missing_explicit_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(str(tmp_path / "nope.yaml"), env={})

    def test_invalid_yaml_raises(self, tmp_path):
        path = write_yaml(tmp_path, "bmc: [unclosed\n")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config_file(path)

    def test_non_mapping_raises(self, tmp_path):
        path = write_yaml(tmp_path, "- just\n- a list\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_config_file(path)

    def test_empty_file_ok(self, tmp_path):
        path = write_yaml(tmp_path, "")
        assert load_config_file(path) == {}


class TestValidation:
    def make_valid(self):
        config = load_config(env={})
        config["bmc"]["host"] = "10.0.0.5"
        return config

    def test_valid_config(self):
        assert validate_config(self.make_valid()) == []

    def test_missing_bmc_host(self):
        errors = validate_config(load_config(env={}))
        assert any("bmc.host" in e for e in errors)

    def test_bad_port_range(self):
        config = self.make_valid()
        config["heartbeat"]["port"] = 99999
        assert any("heartbeat.port" in e for e in errors_of(config))

    def test_negative_interval(self):
        config = self.make_valid()
        config["polling"]["sensor_interval"] = -5
        assert any("polling.sensor_interval" in e for e in errors_of(config))

    def test_bad_log_level(self):
        config = self.make_valid()
        config["agent"]["log_level"] = "CHATTY"
        assert any("log_level" in e for e in errors_of(config))

    def test_peers_require_secret(self):
        config = self.make_valid()
        config["peers"] = [{"host": "10.0.1.101", "port": 5150}]
        assert any("heartbeat.secret" in e for e in errors_of(config))
        config["heartbeat"]["secret"] = "s3cret"
        assert errors_of(config) == []

    def test_peer_missing_host(self):
        config = self.make_valid()
        config["heartbeat"]["secret"] = "s3cret"
        config["peers"] = [{"port": 5150}]
        assert any("peers[0]" in e for e in errors_of(config))

    def test_bad_debounce_pair(self):
        config = self.make_valid()
        config["debounce"]["critical"] = [3, 2]  # threshold > window
        assert any("debounce.critical" in e for e in errors_of(config))

    def test_unknown_action_in_allow_list(self):
        config = self.make_valid()
        config["actions"]["allow_list"] = ["IDENTIFY_LED", "POWER_CYCLE"]
        assert any("POWER_CYCLE" in e for e in errors_of(config))

    def test_site_manager_requires_token_and_ca_with_tls(self):
        config = self.make_valid()
        config["site_manager"]["host"] = "10.0.0.9"
        errors = errors_of(config)
        assert any("site_manager.token" in e for e in errors)
        assert any("site_manager.tls_ca" in e for e in errors)
        config["site_manager"]["token"] = "site-secret"
        config["site_manager"]["tls_ca"] = "/etc/harkeniq/ca.pem"
        assert errors_of(config) == []

    def test_site_manager_tls_off_allows_bare(self):
        config = self.make_valid()
        config["site_manager"]["host"] = "10.0.0.9"
        config["site_manager"]["tls"] = False
        assert errors_of(config) == []

    def test_site_manager_intervals_positive(self):
        config = self.make_valid()
        config["site_manager"]["heartbeat_interval"] = 0
        config["site_manager"]["action_poll_interval"] = -1
        errors = errors_of(config)
        assert any("site_manager.heartbeat_interval" in e for e in errors)
        assert any("site_manager.action_poll_interval" in e for e in errors)


def errors_of(config):
    return validate_config(config)


class TestRender:
    def test_secrets_redacted(self):
        config = load_config(env={})
        config["bmc"]["password"] = "hunter2"
        config["heartbeat"]["secret"] = "sitekey"
        config["site_manager"]["token"] = "sm-token-xyz"
        out = render_config(config)
        assert "hunter2" not in out
        assert "sitekey" not in out
        assert "sm-token-xyz" not in out
        assert "********" in out

    def test_no_redact(self):
        config = load_config(env={})
        config["bmc"]["password"] = "hunter2"
        assert "hunter2" in render_config(config, redact=False)


class TestCli:
    def test_config_show(self, tmp_path):
        path = write_yaml(tmp_path, "bmc:\n  host: 10.0.0.5\n  password: hunter2\n")
        result = CliRunner().invoke(cli_main, ["config", "show", "--config", path])
        assert result.exit_code == 0
        assert "10.0.0.5" in result.output
        assert "hunter2" not in result.output

    def test_config_validate_ok(self, tmp_path):
        path = write_yaml(tmp_path, "bmc:\n  host: 10.0.0.5\n")
        result = CliRunner().invoke(cli_main, ["config", "validate", "--config", path])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_config_validate_fails(self, tmp_path):
        path = write_yaml(tmp_path, "heartbeat:\n  port: -1\n")
        result = CliRunner().invoke(cli_main, ["config", "validate", "--config", path])
        assert result.exit_code == 3
        assert "ERROR" in result.output

    def test_config_validate_missing_file(self, tmp_path):
        result = CliRunner().invoke(
            cli_main, ["config", "validate", "--config", str(tmp_path / "nope.yaml")]
        )
        assert result.exit_code == 3

    def test_config_init(self, tmp_path):
        out = tmp_path / "config.yaml"
        result = CliRunner().invoke(
            cli_main,
            ["config", "init", "-o", str(out), "--bmc-host", "10.0.0.5",
             "--bmc-user", "admin", "--bmc-pass", "pw", "--name", "rack-1"],
        )
        assert result.exit_code == 0, result.output
        loaded = load_config(str(out), env={})
        assert loaded["bmc"]["host"] == "10.0.0.5"
        assert loaded["agent"]["name"] == "rack-1"
        assert validate_config(loaded) == []

    def test_config_init_refuses_overwrite(self, tmp_path):
        out = tmp_path / "config.yaml"
        out.write_text("bmc: {}\n")
        result = CliRunner().invoke(
            cli_main,
            ["config", "init", "-o", str(out), "--bmc-host", "x",
             "--bmc-user", "u", "--bmc-pass", "p", "--name", "n"],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output
