-- HarkenIQ full-stack database initialization.
-- Creates separate databases for each service on a shared PostgreSQL instance.
-- The default database (harkeniq) is created by POSTGRES_DB env var.

CREATE DATABASE harkeniq_sm OWNER harkeniq;
CREATE DATABASE harkeniq_cc OWNER harkeniq;
CREATE DATABASE harkeniq_console OWNER harkeniq;
CREATE DATABASE keycloak OWNER harkeniq;
