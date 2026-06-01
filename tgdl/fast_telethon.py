"""
Parallel chunk downloader for Telethon.

Telethon's built-in download_media() uses a single MTProto connection,
which Telegram throttles at ~300 KB/s per connection regardless of
Premium status. Premium's speed boost only manifests when saturating
multiple parallel connections. This module opens N senders to the
same DC and downloads disjoint chunks concurrently.
"""

import asyncio
import inspect
import math
from typing import BinaryIO, Callable, Optional

from telethon import TelegramClient, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import GetFileRequest


class _DownloadSender:
    def __init__(self, client, sender, file, offset, limit, stride, count):
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self):
        if not self.remaining:
            return None
        result = await self.client._call(self.sender, self.request)
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    def disconnect(self):
        return self.sender.disconnect()


class ParallelTransferrer:
    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None):
        self.client = client
        self.dc_id = dc_id or client.session.dc_id
        self.auth_key = (
            None
            if dc_id and client.session.dc_id != dc_id
            else client.session.auth_key
        )
        self.senders = None

    # Small files don't benefit from many connections (handshake overhead > gain).
    _THREAD_LEVELS = [
        (1 * 1024 * 1024, 1),    # <1 MB  → 1 thread
        (5 * 1024 * 1024, 2),    # <5 MB  → 2 threads
        (20 * 1024 * 1024, 4),   # <20 MB → 4 threads
        (50 * 1024 * 1024, 8),   # <50 MB → 8 threads
    ]

    @classmethod
    def _get_connection_count(cls, file_size: int, max_count: int = 4) -> int:
        """Pick thread count adaptive to file size, capped at max_count."""
        for size, threads in cls._THREAD_LEVELS:
            if file_size < size:
                return min(threads, max_count)
        return max_count  # files >= 50 MB get the full cap

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes
            )
            await sender.send(
                InvokeWithLayerRequest(LAYER, self.client._init_request)
            )
            self.auth_key = sender.auth_key
        return sender

    async def _create_download_sender(self, file, index, part_size, stride, part_count):
        return _DownloadSender(
            self.client,
            await self._create_sender(),
            file,
            index * part_size,
            part_size,
            stride,
            part_count,
        )

    async def _init_download(self, connections, file, part_count, part_size):
        minimum, remainder = divmod(part_count, connections)

        def get_part_count():
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        # Create first sender sequentially so auth_key is populated
        # before the parallel spawn of the rest
        self.senders = [
            await self._create_download_sender(
                file, 0, part_size, connections * part_size, get_part_count()
            ),
            *await asyncio.gather(
                *[
                    self._create_download_sender(
                        file, i, part_size, connections * part_size, get_part_count()
                    )
                    for i in range(1, connections)
                ]
            ),
        ]

    async def download(self, file, file_size, part_size_kb=None, connection_count=None):
        max_conn = connection_count or 4
        connection_count = self._get_connection_count(file_size, max_count=max_conn)
        # Default to 1 MB — the max part size Telegram's API allows
        # (must divide 1 MB and be a multiple of 4 KB; 1024 KB is both).
        part_size = (part_size_kb or 1024) * 1024
        part_count = math.ceil(file_size / part_size)
        await self._init_download(connection_count, file, part_count, part_size)
        part = 0
        try:
            while part < part_count:
                # Fire one request per sender, then await them all so no task
                # is left pending if a sender returns empty early.
                tasks = [asyncio.ensure_future(s.next()) for s in self.senders]
                results = await asyncio.gather(*tasks)
                for data in results:
                    if not data:
                        return
                    yield data
                    part += 1
        finally:
            await asyncio.gather(*[s.disconnect() for s in self.senders])
            self.senders = None


async def fast_download(
    client: TelegramClient,
    message,
    out: BinaryIO,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    connection_count: Optional[int] = None,
) -> int:
    """Download a message's media using parallel MTProto senders.

    Returns total bytes written. `message` must be a Telethon Message
    with document or photo media.
    """
    size = message.file.size
    dc_id, location = utils.get_input_location(message.media)
    downloader = ParallelTransferrer(client, dc_id)
    written = 0
    async for chunk in downloader.download(location, size, connection_count=connection_count):
        out.write(chunk)
        written += len(chunk)
        if progress_callback:
            r = progress_callback(written, size)
            if inspect.isawaitable(r):
                await r
    return written
