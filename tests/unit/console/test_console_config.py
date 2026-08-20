"""ConsoleConfig loading and validation."""

from harkeniq_console.config import ConsoleConfig, _coerce, load_console_config


class TestDefaults:
    def test_defaults(self):
        config = load_console_config(env={})
        assert config.http_port == 8100
        assert config.insecure is False
        assert config.dsn == "sqlite+aiosqlite:///:memory:"
        assert config.keycloak_admin_user == "admin"
        assert config.platform_client_id == "harkeniq-console"
        assert config.support_email_from == "support@harkeniq.com"


class TestEnvOverrides:
    def test_env_overrides(self):
        config = load_console_config(env={
            "HARKEN_CONSOLE_HTTP_PORT": "9100",
            "HARKEN_CONSOLE_INSECURE": "true",
            "HARKEN_CONSOLE_KEYCLOAK_ADMIN_PASSWORD": "secret",
            "HARKEN_CONSOLE_PLATFORM_REALM": "custom-realm",
            "HARKEN_CONSOLE_STRIPE_SECRET_KEY": "sk_test_abc",
        })
        assert config.http_port == 9100
        assert config.insecure is True
        assert config.keycloak_admin_password == "secret"
        assert config.platform_realm == "custom-realm"
        assert config.stripe_secret_key == "sk_test_abc"


class TestYaml:
    def test_yaml_then_env(self, tmp_path):
        path = tmp_path / "console.yaml"
        path.write_text(
            "http_port: 9000\nkeycloak_admin_user: superadmin\n"
            "platform_realm: yaml-realm\n"
        )
        config = load_console_config(
            env={"HARKEN_CONSOLE_HTTP_PORT": "9001"}, yaml_path=str(path)
        )
        assert config.keycloak_admin_user == "superadmin"
        assert config.http_port == 9001  # env wins over yaml
        assert config.platform_realm == "yaml-realm"


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
    def test_validate_missing_keycloak_password(self):
        config = ConsoleConfig()
        errors = config.validate()
        assert any("keycloak_admin_password" in e for e in errors)

    def test_validate_insecure_ok(self):
        config = ConsoleConfig(insecure=True)
        assert config.validate() == []

    def test_validate_invalid_port(self):
        config = ConsoleConfig(insecure=True, http_port=0)
        errors = config.validate()
        assert any("http_port" in e for e in errors)


class TestEnvPrefix:
    def test_all_env_vars_start_with_harken_console(self):
        from harkeniq_console.config import _ENV_MAP
        for key in _ENV_MAP:
            assert key.startswith("HARKEN_CONSOLE_"), (
                f"{key} does not start with HARKEN_CONSOLE_"
            )
