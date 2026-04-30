-- Runs once on first container start via docker-entrypoint-initdb.d
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
CREATE EXTENSION IF NOT EXISTS age;
SELECT create_graph('agno');
