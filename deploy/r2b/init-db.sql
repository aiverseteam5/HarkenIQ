-- Create separate databases for each service.
-- The default harkeniq_console DB is created by POSTGRES_DB env var.
CREATE DATABASE harkeniq_cc OWNER harkeniq;
CREATE DATABASE keycloak OWNER harkeniq;
