# Deploy

Este roteiro sobe a API num servidor Linux com Docker. Ele não pressupõe nada
sobre o que mais roda na máquina — só que existe **um proxy reverso terminando
o TLS**, numa rede Docker que este projeto acessa como convidado.

Consumo medido: `api` ~300 MB + `db` ~150 MB, com teto de 1 GiB cada.

> **Pré-requisito de custo:** use uma **chave OpenAI própria e com teto de
> gasto**, dedicada a esta instância. É uma demonstração pública: a conta que
> ela consome não pode ser a mesma de nenhum sistema em produção.

---

## 1. Preparar o `.env` de produção

```bash
ssh <usuario>@<servidor>
sudo mkdir -p /srv/document-intelligence-miner
sudo chown "$USER":"$USER" /srv/document-intelligence-miner
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
| `EDGE_NETWORK` | nome da rede Docker onde vive o proxy reverso |
| `CORS_ALLOW_ORIGINS` | `[]`, a menos que um navegador vá chamar a API |

A aplicação **recusa subir** se qualquer um dos quatro primeiros estiver errado.
A mensagem no boot lista tudo que falta de uma vez.

Vale revisar também os tetos de uso — os padrões são conservadores, mas o que
faz sentido depende de quanto você aceita gastar por dia:

| Variável | Padrão | O que limita |
|---|---|---|
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `60` / `60` | rajada, por cliente |
| `RATE_LIMIT_METERED_DAILY` | `200` | `/upload` + `/ask` por dia, **global** |
| `MAX_UPLOAD_SIZE_MB` | `20` | tamanho do arquivo |

E os da retenção, que não protegem a fatura e sim quem enviou o arquivo — numa
instância pública, documento de estranho é dado de terceiro:

| Variável | Padrão | O que faz |
|---|---|---|
| `RETENTION_MAX_AGE_DAYS` | `7` | idade em que um documento é apagado, com seus chunks |
| `RETENTION_MAX_DOCUMENTS` | `100` | teto do acervo; os mais antigos saem primeiro |
| `RETENTION_SWEEP_INTERVAL_SECONDS` | `3600` | intervalo entre varreduras (a 1ª é no boot) |

A varredura é uma tarefa de fundo do próprio processo da API — não precisa de
entrada no cron. `RETENTION_ENABLED=false` desliga, e o boot registra um
`WARNING` dizendo que o acervo passa a depender de limpeza manual.

E os da observabilidade — sem eles a instância roda muda, e a única testemunha
de um erro é o `docker logs` de quem lembrar de abrir:

| Variável | Padrão | O que faz |
|---|---|---|
| `SENTRY_DSN` | vazio | destino dos eventos de erro; vazio = desligado |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | fração de requisições com tracing |
| `COST_ALERT_THRESHOLD` | `0.8` | fração do teto diário que vira alerta de custo |

O DSN aceita tanto o Sentry SaaS quanto um **GlitchTip** auto-hospedado, que
fala o mesmo protocolo — útil se preferir não mandar erro nenhum para fora da
máquina. Os eventos saem sem PII e sem variáveis locais; veja o porquê no
README, em *Observabilidade*.

## 2. A rede do proxy reverso

Só um processo pode escutar na porta 443, então o proxy é infraestrutura da
máquina — não deste projeto nem de nenhum outro que divida o servidor. O
`docker-compose.prod.yml` entra na rede dele como convidado: **não a cria e não
a remove**.

```bash
# Uma vez por servidor, se ainda não existir:
docker network create edge

# Confirme o nome do que já existe e ponha em EDGE_NETWORK:
docker network ls
```

Com um Caddy, o bloco correspondente é:

```caddyfile
dim.exemplo.com {
	encode gzip
	# Ver a nota abaixo: este teto é para o corpo que NÃO declara tamanho.
	request_body {
		max_size 21000000
	}
	reverse_proxy dim_api:8000
}
```

> `dim_api` só resolve se o container do proxy estiver na rede `EDGE_NETWORK`.
> A ordem, porém, não importa: medido, o Caddy carrega a config mesmo com o
> upstream inexistente — os outros sites seguem servindo e só este hostname
> devolve `502` até a aplicação subir.

Os dois tetos — o do proxy e o da aplicação — **não** fazem a mesma coisa, e a
diferença foi medida, não deduzida:

| | corta quando | pega o quê |
|---|---|---|
| Aplicação (`MAX_UPLOAD_SIZE_MB` + folga = 21 MiB) | lê o `Content-Length`, **antes** de qualquer byte de corpo | o cliente honesto, que é o caso comum |
| Caddy (`max_size`) | durante a **leitura** do corpo | corpo sem `Content-Length` (`chunked`) ou com tamanho mentido |

Como a aplicação decide pelo cabeçalho, ela responde primeiro quando o tamanho
vem declarado — e aí devolve o `413` com a mensagem precisa. Nesse caminho o
Caddy ainda está escrevendo o corpo quando a aplicação responde e fecha a
conexão, e **de vez em quando ele converte o resultado num `502`**. Em nenhum
dos casos o corpo chega a ser lido; o que varia é só o código que o cliente vê,
numa requisição que já estava sendo recusada.

O `21000000` fica logo acima do maior upload legítimo (20 MiB = 20 971 520) e
abaixo do teto da aplicação (22 020 096). Em bytes e não `21MB` porque `MB`
decimal ou binário muda o número, e aqui a margem é estreita.

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

Trocando `dim.exemplo.com` pelo domínio real:

```bash
# 200, e sem chave (é o que o monitor externo usa)
curl -si https://dim.exemplo.com/health | head -1

# 401 — a proteção está ligada
curl -so /dev/null -w '%{http_code}\n' https://dim.exemplo.com/api/v1/documents

# 200 — com a chave do .env
curl -so /dev/null -w '%{http_code}\n' \
  -H "X-API-Key: $API_KEY" https://dim.exemplo.com/api/v1/documents

# 404 — a documentação interativa não existe em produção
curl -so /dev/null -w '%{http_code}\n' https://dim.exemplo.com/docs

# a retenção subiu junto com a API (uma linha por boot)
docker logs dim_api 2>&1 | grep -i retencao
# retencao ligada: 7 dia(s) de idade, teto de 100 documentos, varredura a cada 3600s

# a observabilidade subiu (ou avisa que está desligada)
docker logs dim_api 2>&1 | grep -i sentry
# sentry ligado (env=production, traces=0.0)

# 429 depois de passar do teto — o limite de uso está ativo
for _ in $(seq 70); do
  curl -so /dev/null -w '%{http_code} ' -H "X-API-Key: $API_KEY" \
    https://dim.exemplo.com/api/v1/documents
done; echo
```

E o que **não** deve aparecer — nenhuma porta nova publicada no host:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep dim_
# dim_api        8000/tcp     <- porta interna, sem "0.0.0.0:"
# dim_postgres   5432/tcp
```

## 5. Backup

`scripts/pg_backup.sh` é o backup deste projeto, e só dele. No cron:

```bash
0 3 * * * /srv/document-intelligence-miner/scripts/pg_backup.sh >> /var/log/dim-backup.log 2>&1
```

Configurável por ambiente: `BACKUP_DIR`, `BACKUP_RETENTION_DAYS` (14) e
`BACKUP_REMOTE` — um destino `rclone`, sem o qual a cópia fica no mesmo disco do
banco, que é justamente o que costuma falhar.

Vale notar a tensão com a retenção do acervo: um documento apagado pela
varredura continua dentro dos dumps já feitos até o último deles sair pela
rotação. Na prática o prazo real de guarda é `RETENTION_MAX_AGE_DAYS` +
`BACKUP_RETENTION_DAYS`. Se o compromisso publicado for o prazo curto, é
`BACKUP_RETENTION_DAYS` que precisa encolher.

**Teste a restauração antes de precisar dela:**

```bash
gunzip -c dim-<carimbo>.sql.gz | docker exec -i dim_postgres psql -U dim -d dim_db
```

## Armadilhas conhecidas

- **`docker system prune`** agendado (comum em servidor compartilhado) apaga
  imagens sem container em uso. Se este projeto ficar parado além da janela do
  prune, a imagem some e o próximo `up` reconstrói. É incômodo, não é perda de
  dado.
- **Trocar `EMBEDDING_MODEL` exige nova revisão do Alembic** e reprocessar o
  acervo inteiro: a dimensão é fixa na coluna e no índice HNSW.
- **O índice HNSW é criado com a tabela vazia**, onde custa milissegundos. Criar
  o mesmo índice com o acervo cheio trava a tabela por minutos.
- **Ingestão é síncrona**: o `POST /upload` só responde quando os embeddings
  estão no banco. Um PDF grande segura a conexão. Fila é a Fase 3.
- **Os contadores de limite vivem em memória.** Reiniciar o container zera o
  teto diário, e subir mais de um worker do uvicorn multiplicaria o teto pelo
  número de workers. O `Dockerfile` sobe um worker só de propósito; passar disso
  exige mover os contadores para o Postgres ou um Redis.
