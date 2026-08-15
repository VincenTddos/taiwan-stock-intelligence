-- Runs once, on first cluster initialisation.
--
-- Extensions are also created idempotently by Alembic migration 0001, which is
-- the authoritative path. This file exists so that a freshly-created cluster is
-- already correct before the first migration runs, and so that the test
-- database gets the same extensions without a separate step.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Separate database for the test suite so `pytest` can drop and recreate
-- schemas without touching development data.
SELECT 'CREATE DATABASE twquant_test OWNER ' || current_user
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'twquant_test') \gexec

\connect twquant_test
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
