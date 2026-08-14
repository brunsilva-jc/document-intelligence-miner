-- Executado uma unica vez, na criacao do cluster (volume pgdata vazio).
-- A aplicacao tambem garante a extensao no startup (app/db/session.py),
-- para o caso de o banco ser provisionado fora do docker-compose.
CREATE EXTENSION IF NOT EXISTS vector;
