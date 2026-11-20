"""Keep one admitted document operation alive until it completes or is cancelled."""

import asyncio
import logging

from ..settings import get_settings
from .repository import DocumentLeaseLost, DocumentRepository


class DocumentExecution:
    """Keep one document claim alive until the operation finishes or is cancelled."""

    def __init__(self, documents: DocumentRepository, document_id: str) -> None:
        self.documents = documents
        self.document_id = document_id
        self.token: str | None = None
        self._interval = get_settings().heartbeat.interval_seconds
        self._heartbeat = None
        self._lost = False

    async def claim(self) -> None:
        claim = asyncio.create_task(
            asyncio.to_thread(self.documents.claim, self.document_id)
        )
        try:
            self.token = await asyncio.shield(claim)
        except asyncio.CancelledError:
            self.token = await claim
            await asyncio.to_thread(self.documents.release, self.token)
            raise

    async def call(self, operation, *args, **kwargs):
        """Drain in-flight synchronous work before releasing its execution claim."""
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def __aenter__(self):
        self._heartbeat = asyncio.create_task(self._renew(asyncio.current_task()))
        return self

    async def __aexit__(self, *_exc):
        self._heartbeat.cancel()
        await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self.call(self.documents.release, self.token)
        if self._lost:
            raise DocumentLeaseLost("document execution lease was lost")

    async def _renew(self, worker):
        while True:
            await asyncio.sleep(self._interval)
            try:
                owned = await self.call(self.documents.renew, self.token)
            except Exception:
                logging.getLogger(__name__).exception("document claim heartbeat failed")
                continue
            if not owned:
                self._lost = True
                worker.cancel()
                return
