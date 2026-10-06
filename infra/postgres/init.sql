-- Runs once when the Postgres volume is first created.
CREATE EXTENSION IF NOT EXISTS vector;

-- Separate database for the test suite (tests truncate tables freely).
CREATE DATABASE cibud_test;
\c cibud_test
CREATE EXTENSION IF NOT EXISTS vector;
