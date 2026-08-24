"""Fixtures compartilhadas.

Os testes desta fase nao tocam o banco: usamos ASGITransport, que nao
dispara o lifespan da aplicacao (e portanto nao chama `init_db`).
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.rate_limit import reset_rate_limits
from app.main import create_app


@pytest.fixture(autouse=True)
def limites_zerados() -> Iterator[None]:
    """Cada teste comeca e termina com os contadores de uso limpos.

    Os limitadores vivem pelo processo inteiro — e justamente isso que os
    faz funcionar em producao. Sem zerar aqui, um teste gastaria a cota do
    seguinte e a suite passaria a depender da ordem de execucao.
    """
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
