"""Fixtures compartilhadas.

Os testes desta fase nao tocam o banco: usamos ASGITransport, que nao
dispara o lifespan da aplicacao (e portanto nao chama `init_db`).
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
