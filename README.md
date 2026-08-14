# Document Intelligence Miner

API REST de **RAG (Retrieval-Augmented Generation)** sobre documentos: você envia
PDFs ou textos, o sistema extrai o conteúdo, divide em blocos (*chunks*), gera
*embeddings* e armazena tudo no PostgreSQL com **pgvector**. Depois, perguntas em
linguagem natural são respondidas por um LLM usando apenas os trechos mais
relevantes do seu acervo — com citação das fontes.

> **Status:** Fases 1 e 2 concluídas — infraestrutura, banco, ingestão com
> chunking + embeddings e o motor de RAG completo. Falta apenas configurar a
> `OPENAI_API_KEY` para usar `/ask` com um LLM real.

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
│       └── rag_engine.py          # busca semântica + prompt + LLM
├── docker/postgres/init.sql       # CREATE EXTENSION vector
├── tests/
├── Dockerfile                     # build multi-stage, usuário não-root
├── docker-compose.yml
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

| Método | Rota | Descrição | Status |
| --- | --- | --- | --- |
| `GET` | `/health` | Liveness (não toca no banco) | ✅ |
| `GET` | `/health/ready` | Readiness (verifica PostgreSQL) | ✅ |
| `POST` | `/api/v1/documents/upload` | Ingestão: extração → chunking → embeddings | ✅ |
| `POST` | `/api/v1/documents/ask` | Pergunta com RAG, retorna resposta + fontes | ✅ |
| `GET` | `/api/v1/documents` | Lista documentos (paginado) | ✅ |
| `GET` | `/api/v1/documents/{id}` | Detalhe do documento | ✅ |
| `DELETE` | `/api/v1/documents/{id}` | Remove documento e seus chunks | ✅ |

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
suportado, `413` arquivo grande demais, `422` sem texto extraível, `404` nenhum
trecho relevante, `502` falha do provedor externo.

---

## Configuração

Todas as variáveis estão documentadas em `.env.example`. As mais relevantes:

| Variável | Padrão | Observação |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `openai` | `openai` ou `huggingface` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | |
| `EMBEDDING_DIM` | `1536` | **deve** casar com o modelo (MiniLM-L6-v2 → 384) |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `150` | em caracteres |
| `RETRIEVAL_TOP_K` | `4` | blocos enviados ao LLM |
| `RETRIEVAL_MAX_DISTANCE` | `0.6` | corte de relevância (0 = idêntico) |

Embeddings locais via HuggingFace (`sentence-transformers` + `torch`, ~2 GB)
ficam em `requirements-ml.txt`, fora da imagem padrão — instale apenas se
`EMBEDDING_PROVIDER=huggingface`.

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

## Limitações conhecidas

- **PDFs digitalizados** (imagem pura) não têm texto extraível e são rejeitados
  com `422`. OCR exigiria Tesseract na imagem — fora do escopo por ora.
- **Ingestão síncrona:** o cliente espera o upload inteiro ser vetorizado.
  Aceitável para documentos de dezenas de páginas; acervos grandes pedem fila.
- **Sem reranking:** a resposta usa os `top_k` vizinhos mais próximos
  diretamente. Um cross-encoder melhoraria a precisão do contexto.
- **Trocar `EMBEDDING_DIM`** invalida todo o acervo: exige migração da coluna e
  reprocessamento dos documentos já ingeridos.

## Roadmap

- [x] Fase 1 — Docker, PostgreSQL + pgvector, esqueleto da API, CI
- [x] Fase 2 — extração de PDF, chunking, embeddings, busca por similaridade e RAG
- [ ] Fase 3 — migrações Alembic, ingestão assíncrona em background, autenticação
- [ ] Fase 4 — reranking, busca híbrida (BM25 + vetorial), OCR
