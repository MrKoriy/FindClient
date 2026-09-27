"""Общие фикстуры тестов: временная база бота и отключение внешних API CRM.

Внешние HTTP-вызовы (TypeSafe Jev, B.AI) в тестах замоканы всегда: suite не
должен зависеть от сети, ключей и лимитов провайдера.
"""

import pytest
import pytest_asyncio

from db.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    """Временная база бота с реальной схемой (миграции выполняются при connect)."""
    d = Database(path=str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


@pytest.fixture(autouse=True)
def _mock_crm_network(monkeypatch):
    """Тесты CRM не ходят в TypeSafe/B.AI: остаётся детерминированный rules-движок."""

    async def _none(*args, **kwargs):
        return None

    from crm import bai, offer

    monkeypatch.setattr(offer, "select_best_with_jev", _none)
    monkeypatch.setattr(offer, "_call_typesafe_jev", _none)
    monkeypatch.setattr(bai, "call_bai_chat", _none)
    monkeypatch.setattr(bai, "generate_bai_offer", _none)
    monkeypatch.setattr(bai, "humanize_with_bai", _none)
