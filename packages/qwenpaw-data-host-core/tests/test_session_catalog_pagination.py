import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories.session import SessionRepository


@pytest.mark.asyncio
async def test_catalog_filters_before_count_and_pagination():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as db:
            identity = Identity(
                user_id="user", tenant_id="tenant", workspace_id="workspace"
            )
            repo = SessionRepository(db, identity)
            for number in range(13):
                session = Session.create(identity=identity, title=f"catalog {number}")
                session.chat_count = 0 if number == 0 else number
                await repo.add(session)
            first, total = await repo.list(
                min_chat_count=1, page=1, page_size=10, sort="chat_count_desc"
            )
            second, second_total = await repo.list(
                min_chat_count=1, page=2, page_size=10, sort="chat_count_desc"
            )
            assert total == second_total == 12
            assert [item.chat_count for item, _ in first] == list(range(12, 2, -1))
            assert [item.chat_count for item, _ in second] == [2, 1]
            _, unfiltered_total = await repo.list()
            assert unfiltered_total == 13
            matched, matched_total = await repo.list(
                min_chat_count=1, search_text="catalog 12", page_size=10
            )
            assert matched_total == len(matched) == 1
            targeted, targeted_total = await repo.list(
                session_id=matched[0][0].id, page_size=1
            )
            assert targeted_total == 1
            assert targeted[0][0].id == matched[0][0].id
            missing, missing_total = await repo.list(
                session_id="not-owned-or-missing", page_size=1
            )
            assert missing == []
            assert missing_total == 0
    finally:
        await engine.dispose()
