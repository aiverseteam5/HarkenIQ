-- HarkenIQ full-stack database initialization.
-- Creates separate databases for each service on a shared PostgreSQL instance.
-- The default database (harkeniq) is created by POSTGRES_DB env var.

CREATE DATABASE harkeniq_sm OWNER harkeniq;
-- E1.3: a SECOND Site Manager, for the multi-site gate. Two Site
-- Managers never share a database: each is the execution and safety
-- boundary for the sites it serves.
CREATE DATABASE harkeniq_sm2 OWNER harkeniq;
CREATE DATABASE harkeniq_cc OWNER harkeniq;
CREATE DATABASE harkeniq_console OWNER harkeniq;
CREATE DATABASE keycloak OWNER harkeniq;
