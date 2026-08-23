# Deploy no servidor compartilhado

Este projeto sobe na **mesma máquina que o `services-orchestrator`** (Contabo
VPS, 6 vCPU / 11 GiB). O orchestrator é trabalho pago e tem prioridade: tudo
aqui é escrito para não competir com ele.

Consumo medido do que está aqui: `api` ~300 MB + `db` ~150 MB, com teto de 1 GiB
cada. Sobra folga confortável na máquina.

> **Pré-requisito de custo:** use uma **chave OpenAI própria, com teto de gasto**
> — nunca a chave da geração de livros. Uma chave estourada aqui pararia pedido
> pago lá.

---

## 1. Preparar o `.env` de produção

```bash
ssh deploy@169.58.204.164
sudo mkdir -p /srv/document-intelligence-miner
sudo chown deploy:deploy /srv/document-intelligence-miner
git clone <repo> /srv/document-intelligence-miner
cd /srv/document-intelligence-miner
cp .env.example .env
```

Gere a chave da API e a senha do banco:

```bash
python3 -c "import secrets; print('API_KEY=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
```

O `.env` precisa ter, no mínimo:

| Variável | Valor |
|---|---|
| `ENVIRONMENT` | `production` |
| `API_KEY` | a gerada acima (≥ 32 caracteres) |
| `POSTGRES_PASSWORD` | a gerada acima (**não** `dim_secret`) |
| `OPENAI_API_KEY` | chave própria, com teto de gasto |
| `CORS_ALLOW_ORIGINS` | `[]`, a menos que um navegador vá chamar a API |

A aplicação **recusa subir** se qualquer um dos quatro primeiros estiver errado.
A mensagem no boot lista tudo que falta de uma vez.

## 2. Rotear pelo Caddy do orchestrator

O Caddy que já está no ar termina o TLS. `dim.169-58-204-164.sslip.io` **já
resolve** para o IP do servidor (o sslip.io aceita rótulo antes do IP), então
não há DNS a configurar.

Em `services-orchestrator/deploy/Caddyfile`, acrescente um segundo bloco:

```caddyfile
dim.{$DOMAIN} {
	encode gzip
	reverse_proxy dim_api:8000
}
```

E recarregue **sem derrubar o orchestrator**:

```bash
cd /srv/services-orchestrator
docker compose --env-file .env -f deploy/compose.yml exec caddy \
  caddy reload --config /etc/caddy/Caddyfile
```

> O nome `dim_api` só resolve porque o `docker-compose.prod.yml` entra na rede
> `services-orchestrator_default`. Suba este projeto **antes** de recarregar o
> Caddy — apontar para um nome que não existe faz o Caddy recusar a config.

## 3. Subir

```bash
cd /srv/document-intelligence-miner
docker compose --env-file .env -f docker-compose.prod.yml up -d --build
```

A flag `--env-file` é obrigatória (sem ela o Compose procura o `.env` ao lado do
arquivo). A ordem é garantida pelo próprio Compose: o banco fica saudável, o
serviço `migrate` roda `alembic upgrade head` e sai, e só então a API sobe. Se a
migração falhar, **a API não sobe** — em vez de subir e responder 500 em tudo.

## 4. Conferir

```bash
# 200, e sem chave (é o que o monitor externo usa)
curl -si https://dim.169-58-204-164.sslip.io/health | head -1

# 401 — a proteção está ligada
curl -so /dev/null -w '%{http_code}\n' https://dim.169-58-204-164.sslip.io/api/v1/documents

# 200 — com a chave do .env
curl -so /dev/null -w '%{http_code}\n' \
  -H "X-API-Key: $API_KEY" https://dim.169-58-204-164.sslip.io/api/v1/documents

# 404 — a documentação interativa não existe em produção
curl -so /dev/null -w '%{http_code}\n' https://dim.169-58-204-164.sslip.io/docs
```

E o que **não** deve aparecer — nenhuma porta nova publicada no host:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep dim_
# dim_api        8000/tcp     <- porta interna, sem "0.0.0.0:"
# dim_postgres   5432/tcp
```

## 5. Backup

O `pg_backup.sh` do orchestrator só faz backup do banco dele. Sem o passo
abaixo, **o acervo aqui não tem backup nenhum**.

Em `services-orchestrator/scripts/pg_backup.sh`, acrescente o dump deste banco
(mesmo destino no R2, prefixo próprio):

```bash
docker exec dim_postgres pg_dump -U dim dim_db | gzip > "$TMP/dim-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
```

> Confira os nomes de variável do script antes de colar — o trecho acima segue
> o formato dele, mas o script é a fonte da verdade.

## Armadilhas conhecidas

- **`docker system prune -af --filter "until=168h"`** roda todo domingo no cron
  do servidor. Ele apaga imagens **sem container em uso** — se este projeto
  ficar parado mais de 7 dias, a imagem some e o próximo `up` reconstrói. É
  incômodo, não é perda de dado.
- **Trocar `EMBEDDING_MODEL` exige nova revisão do Alembic** e reprocessar o
  acervo inteiro: a dimensão é fixa na coluna e no índice HNSW.
- **O índice HNSW é criado com a tabela vazia**, onde custa milissegundos. Criar
  o mesmo índice com o acervo cheio trava a tabela por minutos.
- **Ingestão é síncrona**: o `POST /upload` só responde quando os embeddings
  estão no banco. Um PDF grande segura a conexão. Fila é a Fase 3.
