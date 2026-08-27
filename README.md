# Document Intelligence Miner

API REST de **RAG (Retrieval-Augmented Generation)** sobre documentos: você envia
PDFs ou textos, o sistema extrai o conteúdo, divide em blocos (*chunks*), gera
*embeddings* e armazena tudo no PostgreSQL com **pgvector**. Depois, perguntas em
linguagem natural são respondidas por um LLM usando apenas os trechos mais
relevantes do seu acervo — com citação das fontes.

> **Status:** Fases 1 e 2 concluídas — infraestrutura, banco, ingestão com
> chunking + embeddings e o motor de RAG completo. Da Fase 3 já entraram
> **migrações Alembic**, **autenticação por chave de API**, **limites de
> uso** (rajada, teto diário de gasto e teto de corpo da requisição),
> **retenção do acervo** e **observabilidade** (erro agregado + alerta de
> custo), o que torna o projeto implantável fora da máquina
> do desenvolvedor — veja
> [`docs/DEPLOY.md`](docs/DEPLOY.md). Falta configurar a `OPENAI_API_KEY` para
> usar `/ask` com um LLM real.

---

## Arquitetura

Separação em camadas no espírito da Clean Architecture — cada camada só conhece
a de baixo, e o domínio não sabe o que é HTTP:

```
HTTP ──▶ API (routes/deps)        valida entrada, serializa saída
           │
           ▼
        Services                  regra de negócio, orquestração do pipeline
           │
           ▼
        Repositories              todo o SQL, encapsula o pgvector
           │
           ▼
        Models (ORM) ──▶ PostgreSQL + pgvector
```

Fluxo de ingestão e consulta:

```
POST /documents/upload
  arquivo ─▶ extração (pypdf) ─▶ chunking ─▶ embeddings ─▶ document_chunks

POST /documents/ask
  pergunta ─▶ embedding ─▶ busca cosine (pgvector) ─▶ top-k chunks
           ─▶ prompt com contexto ─▶ LLM ─▶ resposta + fontes
```

### Estrutura de pastas

```
document-intelligence-miner/
├── .github/workflows/ci.yml       # lint + testes + build da imagem
├── app/
│   ├── main.py                    # application factory, lifespan, middlewares
│   ├── __version__.py
│   ├── api/
│   │   ├── deps.py                # injeção de dependência (session, repos, services)
│   │   └── routes.py              # /health, /documents/*
│   ├── core/
│   │   ├── config.py              # Settings (Pydantic BaseSettings)
│   │   ├── exceptions.py          # erros de domínio + handlers HTTP
│   │   ├── security.py            # chave de API (header X-API-Key)
│   │   ├── rate_limit.py          # tetos de rajada e de gasto diário
│   │   ├── body_limit.py          # teto de corpo, antes de o corpo ser lido
│   │   ├── observability.py       # erro agregado (Sentry) + alerta de custo
│   │   └── logging.py
│   ├── db/
│   │   ├── base.py                # DeclarativeBase + TimestampMixin
│   │   └── session.py             # engine async, sessão por request, init_db
│   ├── models/
│   │   ├── domain.py              # Document, DocumentChunk (coluna Vector)
│   │   └── schemas.py             # contratos de I/O (Pydantic)
│   ├── repositories/
│   │   └── document_repository.py # acesso a dados + busca vetorial
│   └── services/
│       ├── document_service.py    # pipeline de ingestão
│       ├── document_processor.py  # extração de PDF/TXT + chunking
│       ├── embeddings.py          # OpenAI / HuggingFace atrás de um Protocol
│       ├── rag_engine.py          # busca semântica + prompt + LLM
│       └── retention.py           # varredura periódica: idade + teto do acervo
├── docker/postgres/init.sql       # CREATE EXTENSION vector
├── docs/DEPLOY.md                 # subir num servidor com Docker
├── migrations/                    # Alembic: env.py + versions/
├── scripts/pg_backup.sh           # dump + rotação + envio para fora
├── tests/
├── alembic.ini
├── Dockerfile                     # build multi-stage, usuário não-root
├── docker-compose.yml             # desenvolvimento
├── docker-compose.prod.yml        # produção (arquivo completo, não override)
└── requirements*.txt
```

Duas adições à estrutura inicialmente proposta, ambas para reforçar a separação
de camadas: `app/repositories/` (o SQL sai do service) e `app/api/deps.py`
(a injeção de dependência sai das rotas).

---

## Modelo de dados

| Tabela | Papel |
| --- | --- |
| `documents` | metadados do arquivo: nome, tipo, tamanho, `checksum` (SHA-256, evita reingestão), `status` do pipeline, contagem de chunks |
| `document_chunks` | texto do bloco + `embedding vector(N)` + `chunk_index`, com `ON DELETE CASCADE` |

A busca usa **distância de cosseno** (operador `<=>` do pgvector), acelerada por
um índice **HNSW** (`vector_cosine_ops`) — melhor recall que IVFFlat e sem
necessidade de treino prévio com dados já na tabela.

A dimensão do vetor é fixa no schema (`EMBEDDING_DIM`). Trocar de modelo de
embedding exige migração da coluna **e** reprocessamento do acervo.

---

## Como rodar

Pré-requisitos: Docker + Docker Compose. (Para rodar fora de container:
Python 3.12+.)

```bash
cp .env.example .env
docker compose up --build
```

Pronto:

- API: <http://localhost:8000>
- Swagger UI: <http://localhost:8000/docs>
- Liveness: <http://localhost:8000/health>
- Readiness (checa o banco): <http://localhost:8000/health/ready>

```bash
curl -s localhost:8000/health/ready
# {"status":"ok","service":"Document Intelligence Miner","version":"0.1.0","database":"up"}
```

Verificando a extensão no banco:

```bash
docker compose exec db psql -U dim -d dim_db -c "\dx vector"
docker compose exec db psql -U dim -d dim_db -c "\d document_chunks"
```

### Desenvolvimento local (sem container para a API)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d db          # só o Postgres
uvicorn app.main:app --reload
```

### Testes e lint

```bash
pytest
black --check app tests && isort --check-only app tests && flake8 app tests
```

---

## Endpoints

| Método | Rota | Descrição | Chave |
| --- | --- | --- | --- |
| `GET`/`HEAD` | `/health` | Liveness (não toca no banco) | — |
| `GET` | `/health/ready` | Readiness (verifica PostgreSQL) | — |
| `POST` | `/api/v1/documents/upload` | Ingestão: extração → chunking → embeddings | 🔑 |
| `POST` | `/api/v1/documents/ask` | Pergunta com RAG, retorna resposta + fontes | 🔑 |
| `GET` | `/api/v1/documents` | Lista documentos (paginado) | 🔑 |
| `GET` | `/api/v1/documents/{id}` | Detalhe do documento | 🔑 |
| `DELETE` | `/api/v1/documents/{id}` | Remove documento e seus chunks | 🔑 |

🔑 = exige o header `X-API-Key` quando `API_KEY` está configurada (obrigatória
fora de `ENVIRONMENT=local`) e conta nos limites de uso, que respondem `429`
com `Retry-After`. O `/health` fica aberto de propósito e fora do limite: o
monitor externo não tem credencial — e responde a `HEAD`, que é como o
UptimeRobot sonda por padrão.

### Exemplos

```bash
# Ingestão — reenviar o mesmo arquivo devolve duplicated: true, sem reprocessar
curl -X POST localhost:8000/api/v1/documents/upload -F "file=@contrato.pdf"
```

```json
{
  "document": { "id": "…", "filename": "contrato.pdf", "status": "completed", "chunk_count": 42 },
  "chunks_created": 42,
  "duplicated": false
}
```

```bash
# Pergunta — a resposta cita os trechos que a fundamentaram
curl -X POST localhost:8000/api/v1/documents/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Qual o prazo de vigência do contrato?", "top_k": 4}'
```

```json
{
  "question": "Qual o prazo de vigência do contrato?",
  "answer": "O prazo de vigência é de 24 meses a partir da assinatura, renovável automaticamente. [1]",
  "sources": [
    { "filename": "contrato.pdf", "page": 3, "score": 0.88, "chunk_index": 12, "content": "…" }
  ],
  "model": "gpt-4o-mini",
  "elapsed_ms": 1840
}
```

Erros usam sempre o mesmo envelope (`{"detail": …, "error": …}`): `415` tipo não
suportado, `413` arquivo (ou corpo) grande demais, `422` sem texto extraível,
`404` nenhum trecho relevante, `429` limite de uso excedido (com `Retry-After`),
`502` falha do provedor externo.

---

## Configuração

Todas as variáveis estão documentadas em `.env.example`. As mais relevantes:

| Variável | Padrão | Observação |
| --- | --- | --- |
| `ENVIRONMENT` | `local` | `staging`/`production` ativam as exigências abaixo |
| `API_KEY` | vazio | header `X-API-Key`; **obrigatória** fora de `local` (≥ 32 car.) |
| `CORS_ALLOW_ORIGINS` | `[]` | só preencha se um navegador for chamar a API |
| `EMBEDDING_PROVIDER` | `openai` | `openai` ou `huggingface` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | |
| `EMBEDDING_DIM` | `1536` | **deve** casar com o modelo (MiniLM-L6-v2 → 384) |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `150` | em caracteres |
| `RETRIEVAL_TOP_K` | `4` | blocos enviados ao LLM |
| `RETRIEVAL_MAX_DISTANCE` | `0.6` | corte de relevância (0 = idêntico) |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `60` / `60` | rajada, por cliente |
| `RATE_LIMIT_METERED_DAILY` | `50` | `/upload` + `/ask` por dia, **global** |
| `RETENTION_MAX_AGE_DAYS` | `7` | idade máxima de um documento no acervo |
| `RETENTION_MAX_DOCUMENTS` | `100` | teto do acervo; os mais antigos saem primeiro |
| `SENTRY_DSN` | vazio | erro agregado; vazio = desligado (serve GlitchTip também) |
| `COST_ALERT_THRESHOLD` | `0.8` | fração do teto diário que dispara alerta de custo |
| `MAX_UPLOAD_SIZE_MB` | `5` | tamanho do arquivo aceito; é o teto de gasto que mais pesa |
| `EDGE_NETWORK` | `edge` | rede do proxy reverso (só em produção) |

Embeddings locais via HuggingFace (`sentence-transformers` + `torch`, ~2 GB)
ficam em `requirements-ml.txt`, fora da imagem padrão — instale apenas se
`EMBEDDING_PROVIDER=huggingface`.

### Segurança

As rotas de documentos são *metered*: cada `/upload` e cada `/ask` gastam tokens
pagos no provedor. Por isso o comportamento muda com o `ENVIRONMENT`:

| | `local` | `staging` / `production` |
| --- | --- | --- |
| Chave de API | opcional (vazia = desligada) | **obrigatória**, ≥ 32 caracteres |
| `/docs`, `/redoc`, `/openapi.json` | no ar | fora do ar (404) |
| CORS | `*` | apenas `CORS_ALLOW_ORIGINS` |
| Schema do banco | `metadata.create_all` | Alembic (e a API confere no boot) |
| Senha `dim_secret` | aceita | recusada no boot |
| `OPENAI_API_KEY` ausente | aceita (`/ask` responde 502) | recusada no boot |

As recusas acontecem no **boot**, não na primeira requisição, e a mensagem lista
todos os problemas de uma vez. Uma API que sobe, responde `/health` e parece
saudável enquanto está aberta ou sem schema é o pior dos mundos.

#### Limites de uso

A chave responde *quem* pode chamar; ela não responde *quanto*. Com a chave em
mãos, um laço de `/ask` gasta a `OPENAI_API_KEY` do dono até o teto do cartão —
e uma demonstração pública existe justamente para ser chamada por estranhos.

| Limite | Escopo | Onde |
| --- | --- | --- |
| `RATE_LIMIT_REQUESTS` por janela | por cliente (chave, ou IP sem chave) | todas as rotas `/documents` |
| `RATE_LIMIT_METERED_DAILY` | **global** — a fatura é uma só | `/upload` e `/ask` |
| `MAX_UPLOAD_SIZE_MB` + folga | por requisição | qualquer corpo |

O teto de corpo é aplicado **antes** de o corpo ser lido
(`app/core/body_limit.py`): recusa pelo `Content-Length` declarado e, quando ele
não existe (`chunked`) ou mente, corta pela contagem do que chega. A validação
de `DocumentProcessor` continua lá, com a mensagem precisa — mas ela só roda
depois de o multipart inteiro ter sido montado, tarde demais numa máquina com
teto de memória.

Os contadores vivem em memória do processo: reiniciar zera o teto diário, e mais
de um worker do uvicorn multiplicaria o teto pelo número de workers. O
`Dockerfile` sobe um worker só de propósito.

#### Retenção do acervo

Os limites acima protegem a fatura. A retenção protege outra coisa: **quem
enviou o arquivo**. Numa demonstração pública o documento é de um estranho — ou
seja, dado de terceiro —, e guardá-lo indefinidamente porque ninguém escreveu a
linha que o apaga também é uma decisão, só que tomada por omissão.

| Regra | Padrão | O que pega |
| --- | --- | --- |
| `RETENTION_MAX_AGE_DAYS` | `7` | todo documento vence — é o limite de *por quanto tempo* |
| `RETENTION_MAX_DOCUMENTS` | `100` | uma enxurrada na mesma janela, nova demais para vencer |

A idade roda **antes** do teto, e o teto conta o que sobreviveu a ela — na ordem
inversa, o teto apagaria por quota o que a idade já apagaria de graça. Apagar o
documento leva os chunks junto (`ON DELETE CASCADE`), sem o que o `/ask`
responderia citando trecho de arquivo já removido.

A varredura roda numa tarefa de fundo do próprio processo
(`app/services/retention.py`), iniciada no `lifespan`, com a primeira passada no
boot: um processo que ficou dias fora do ar volta com documentos já vencidos, e
esperar o primeiro intervalo seria guardar dado alheio justamente no caso em que
ele já passou do prazo. Falha de varredura não derruba o laço — banco fora do ar
adia a limpeza, não a cancela pelo resto da vida do processo.

`RETENTION_ENABLED=false` desliga tudo, e o boot avisa em `WARNING`: sem a
varredura, o acervo só é limpo à mão.

#### Observabilidade

Os logs estruturados respondem *o que aconteceu* para quem já está olhando.
Isto responde outra coisa: **quem avisa**. Numa instância que roda sozinha num
VPS, a única testemunha de um erro é o `docker logs` de quem lembrar de abrir —
ou seja, ninguém, até um estranho reclamar.

| Sinal | Como chega | Por quê |
| --- | --- | --- |
| Exceção não tratada | evento no Sentry, com stack e contexto | o handler global transforma tudo em 500 educado; sem captura explícita, o erro sai invisível |
| Falha da varredura de retenção | `logger.exception` → evento (`LoggingIntegration`) | tarefa de fundo: ninguém está olhando quando ela falha |
| Teto diário em `COST_ALERT_THRESHOLD` | `WARNING` + evento | é o sinal de que a demo virou alvo — e ele passa despercebido porque **nada falhou** |
| Teto diário esgotado | `ERROR` + evento | uma vez por janela, não por requisição recusada |

`SENTRY_DSN` vazio deixa o SDK inerte — nada é enviado, e a aplicação loga
igual. O DSN é configurável de propósito: o mesmo protocolo serve o Sentry SaaS
e um **GlitchTip** auto-hospedado.

**O que não sai daqui.** Um relatório de erro carrega os cabeçalhos da
requisição, e um deles é o `X-API-Key` da demo — mandá-lo para um serviço de
terceiros vaza a credencial em silêncio. Daí `send_default_pii=False` e o filtro
que apaga `X-API-Key`, `Authorization` e `Cookie`.

Filtrar cabeçalho, porém, **não basta** — e isso foi medido, não suposto: com um
coletor local no lugar do Sentry, a chave continuava saindo pelas **variáveis
locais do stack trace**, porque os quadros de uma requisição ASGI carregam o
`scope` inteiro, com os cabeçalhos crus em bytes. Daí
`include_local_variables=False`. O custo é real (o painel deixa de mostrar o
valor das variáveis no momento do erro), e vale mesmo assim: nesta aplicação os
locais guardam também o texto extraído do PDF de um terceiro.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # gera a API_KEY
curl -H "X-API-Key: $API_KEY" localhost:8000/api/v1/documents
```

---

## Migrações (Alembic)

Em `local` o schema nasce do próprio metadata no startup — quem clonou o repo
não precisa saber que o Alembic existe. Fora de `local` o schema é do Alembic, e
o startup apenas **confere** (a API não sobe com as tabelas faltando).

```bash
alembic upgrade head          # aplica  (o serviço `migrate` faz isso no deploy)
alembic downgrade base        # desfaz
alembic check                 # o schema ainda casa com os modelos?
alembic revision --autogenerate -m "descricao"
```

`alembic check` é o que impede a dupla schema/modelo de divergir em silêncio;
vale rodar depois de mexer em `app/models/domain.py`.

Duas particularidades desta base:

- A **primeira revisão foi escrita à mão**: o autogenerate não sabe que a
  extensão `vector` precisa existir antes das colunas que usam o tipo.
- A **dimensão do embedding vem de `settings.EMBEDDING_DIM`**, como no modelo
  ORM. Deixa a revisão dependente do ambiente — o mal menor, já que schema e
  modelo têm de concordar ou todo `INSERT` falha.

---

## CI/CD

`.github/workflows/ci.yml`, disparado em push e PR para `main`:

1. **lint** — `black --check`, `isort --check-only`, `flake8`
2. **test** — `pytest` (só roda se o lint passar)
3. **docker** — valida o build da imagem, com cache do GitHub Actions

---

## Decisões técnicas

- **`asyncpg` + SQLAlchemy 2.0 async** — I/O de banco não bloqueia o event loop,
  que é justamente o gargalo de uma API que também espera por LLM.
- **Sessão por request (*unit of work*)** — commit ao fim de um request bem
  sucedido, rollback em qualquer exceção (`app/db/session.py`).
- **Exceções de domínio** — services levantam `DomainError`; a camada de API as
  traduz para HTTP. O domínio continua testável sem o FastAPI.
- **Imagem multi-stage e usuário não-root** — build separado do runtime, sem
  toolchain de compilação na imagem final.
- **`init_db()` só sincroniza o schema em `ENVIRONMENT=local`** — em
  staging/produção o schema é versionado com Alembic (`alembic upgrade head`).
- **Providers atrás de `Protocol`** — `EmbeddingProvider` e `LLMProvider` são
  contratos estruturais. Trocar OpenAI por HuggingFace, ou por um dublê nos
  testes, não toca em service, repositório ou rota.
- **Nada é escrito antes do processamento terminar** — extração, chunking e
  embeddings acontecem em memória; documento e chunks entram na mesma
  transação. Uma falha no meio não deixa documento órfão no banco.
- **Dedup por checksum SHA-256** — reenviar o mesmo arquivo devolve o documento
  existente com `duplicated: true`, sem pagar de novo pelos embeddings.
- **Dimensão validada antes do INSERT** — um modelo que devolva vetor de tamanho
  diferente falha com mensagem clara, e não com um erro opaco do PostgreSQL.
- **Chunking por página** — dividir cada página separadamente (em vez do texto
  concatenado) mantém a citação precisa e evita blocos que misturam páginas
  distantes.
- **Sem contexto relevante, o LLM não é chamado** — `/ask` retorna `404` em vez
  de pagar por uma resposta que só poderia ser alucinada.
- **Embeddings em lotes de 64** — menos round-trips na ingestão, sem estourar o
  limite de tokens por requisição do provedor.
- **Limite de uso escrito à mão, sem dependência nova** — janela fixa em
  memória (`app/core/rate_limit.py`). Com um worker só, uma dependência a mais
  (e um Redis atrás dela) custaria mais do que resolve.
- **Teto de gasto é global, teto de rajada é por cliente** — somar por cliente
  não protegeria a fatura, que é uma só; e uma rajada de um cliente não deve
  derrubar a experiência dos outros.
- **A identidade do cliente entra nos logs como digest** — identificador de
  cliente é escrito em disco; chave de API em texto claro no log é credencial
  vazada.
- **Corpo grande é cortado, não recusado com exceção** — quem lê o corpo é o
  parser de multipart, que traduz qualquer erro para um `400` genérico. O
  middleware encerra o stream e substitui a resposta, e o `413` deixa de
  depender de como o parser reage a um corpo truncado.

## Limitações conhecidas

- **PDFs digitalizados** (imagem pura) não têm texto extraível e são rejeitados
  com `422`. OCR exigiria Tesseract na imagem — fora do escopo por ora.
- **Ingestão síncrona:** o cliente espera o upload inteiro ser vetorizado.
  Aceitável para documentos de dezenas de páginas; acervos grandes pedem fila.
- **Sem reranking:** a resposta usa os `top_k` vizinhos mais próximos
  diretamente. Um cross-encoder melhoraria a precisão do contexto.
- **Trocar `EMBEDDING_DIM`** invalida todo o acervo: exige migração da coluna e
  reprocessamento dos documentos já ingeridos.
- **Retenção só por idade e quantidade:** não há quota por cliente nem por
  tamanho total em disco, e um documento apagado ainda vive nos backups até o
  dump que o contém sair pelo `BACKUP_RETENTION_DAYS`.
- **Alerta sem histórico de custo:** o aviso diz que o teto está sendo
  consumido, não quanto já se gastou em dólares — não há leitura da fatura do
  provedor, e os contadores morrem com o processo.

## Roadmap

- [x] Fase 1 — Docker, PostgreSQL + pgvector, esqueleto da API, CI
- [x] Fase 2 — extração de PDF, chunking, embeddings, busca por similaridade e RAG
- [ ] Fase 3 — [x] migrações Alembic · [x] autenticação por chave · [x] limites de uso e de corpo · [x] retenção do acervo · [x] observabilidade · [ ] ingestão assíncrona em background
- [ ] Fase 4 — reranking, busca híbrida (BM25 + vetorial), OCR
