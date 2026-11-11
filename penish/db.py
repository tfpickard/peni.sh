from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool


async def _configure(conn: AsyncConnection[DictRow]) -> None:
    # Per connection, because the vector type oid is looked up on that connection.
    await register_vector_async(conn)


def make_pool(
    dsn: str, *, min_size: int = 1, max_size: int = 10
) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
    # open=False: the caller opens it inside the running loop (app lifespan).
    return AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        configure=_configure,
        open=False,
    )
