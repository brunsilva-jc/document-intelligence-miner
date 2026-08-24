#!/usr/bin/env bash
#
# Backup do banco deste projeto. Independente de qualquer outro servico que
# divida a maquina: o que quebra aqui nao pode depender de um script de fora,
# e o que roda aqui nao pode mexer no de ninguem.
#
# Uso (cron do servidor, todo dia as 3h):
#   0 3 * * * /srv/document-intelligence-miner/scripts/pg_backup.sh >> /var/log/dim-backup.log 2>&1
#
# Restaurar (o passo que ninguem testa ate precisar):
#   gunzip -c dim-20260824T030000Z.sql.gz | docker exec -i dim_postgres psql -U dim -d dim_db
#
# ATENCAO: copia no mesmo disco do banco nao e backup — o disco e exatamente
# o que costuma falhar. Defina BACKUP_REMOTE (destino rclone) para que cada
# dump saia da maquina.

set -euo pipefail

CONTAINER="${BACKUP_CONTAINER:-dim_postgres}"
DB_USER="${POSTGRES_USER:-dim}"
DB_NAME="${POSTGRES_DB:-dim_db}"
BACKUP_DIR="${BACKUP_DIR:-/srv/backups/document-intelligence-miner}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
# Ex.: "r2:meu-bucket/dim". Vazio = so copia local.
REMOTE="${BACKUP_REMOTE:-}"

carimbo="$(date -u +%Y%m%dT%H%M%SZ)"
destino="${BACKUP_DIR}/dim-${carimbo}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%FT%TZ)] dump de ${DB_NAME} -> ${destino}"

# `pipefail` importa aqui: sem ele, um pg_dump que falha ainda produz um .gz
# valido (e vazio), e a rotacao logo abaixo apagaria os backups bons para
# ficar com esse.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip >"$destino"

# Cinto e suspensorio: um dump de schema vazio passa pelo pipefail.
tamanho="$(stat -c%s "$destino")"
if [ "$tamanho" -lt 1024 ]; then
	echo "ERRO: dump com ${tamanho} bytes, pequeno demais para ser real. Mantendo os anteriores." >&2
	rm -f "$destino"
	exit 1
fi

if [ -n "$REMOTE" ]; then
	echo "[$(date -u +%FT%TZ)] enviando para ${REMOTE}"
	rclone copy "$destino" "$REMOTE"
fi

# A rotacao so acontece depois de um dump bem-sucedido — nunca antes.
apagados="$(find "$BACKUP_DIR" -name 'dim-*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)"

echo "[$(date -u +%FT%TZ)] ok: ${tamanho} bytes, ${apagados} backup(s) expirado(s) removido(s)"
