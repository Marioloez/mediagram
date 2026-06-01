import asyncio
from datetime import datetime, timezone
from pathlib import Path

from telethon.errors import FloodWaitError
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.tl.types import (
    InputMessagesFilterDocument,
    InputMessagesFilterMusic,
    InputMessagesFilterPhotos,
    InputMessagesFilterPhotoVideo,
    InputMessagesFilterVideo,
    InputMessagesFilterVoice,
)

from tgdl.fast_telethon import fast_download

console = Console()

NATIVE_FILTERS = {
    "photo": InputMessagesFilterPhotos,
    "video": InputMessagesFilterVideo,
    "audio": InputMessagesFilterMusic,
    "voice": InputMessagesFilterVoice,
    "document": InputMessagesFilterDocument,
    "photo-video": InputMessagesFilterPhotoVideo,
}


def _matches(msg: Message, types: list[str]) -> bool:
    if not msg.file:
        return False
    mime = (msg.file.mime_type or "").lower()
    if "photo" in types and (msg.photo or mime.startswith("image/")):
        return True
    if "video" in types and mime.startswith("video/"):
        return True
    if "voice" in types and msg.voice:
        return True
    if "audio" in types and mime.startswith("audio/") and not msg.voice:
        return True
    if "document" in types and msg.document and not mime.startswith(("image/", "video/", "audio/")):
        return True
    return False


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "chat"


def _filename(msg: Message) -> str:
    if msg.file and msg.file.name:
        # Sanitize: Telegram filenames can contain chars illegal on NTFS
        # (: ? * < > | etc.) which would crash the write on Windows.
        return f"{msg.id}_{_safe(msg.file.name)}"
    ext = (msg.file.ext if msg.file and msg.file.ext else "") or ".bin"
    return f"{msg.id}{ext}"


def _human_size(n: int | None) -> str:
    if not n:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f} {units[i]}"


def _human_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "-"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _kind(msg: Message) -> str:
    if not msg.file:
        return "?"
    mime = (msg.file.mime_type or "").lower()
    if msg.voice:
        return "voice"
    if msg.photo or mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "doc"


async def collect_messages(
    client: TelegramClient,
    entity,
    types: list[str] | None,
    limit: int | None,
    from_date: datetime | None,
    to_date: datetime | None,
    message_ids: list[int] | None,
    verbose: bool = False,
) -> list[Message]:
    if message_ids:
        result = await client.get_messages(entity, ids=message_ids)
        return [m for m in result if m and m.file]

    kwargs: dict = {}
    if limit:
        kwargs["limit"] = limit
    if types and len(types) == 1 and types[0] in NATIVE_FILTERS:
        kwargs["filter"] = NATIVE_FILTERS[types[0]]()
    if to_date:
        kwargs["offset_date"] = to_date

    from_utc = from_date.astimezone(timezone.utc) if from_date else None
    results: list[Message] = []
    scanned = 0
    kind_counts: dict[str, int] = {}
    async for msg in client.iter_messages(entity, **kwargs):
        scanned += 1
        if verbose and scanned % 200 == 0:
            console.print(f"[dim]scanned {scanned} messages, kept {len(results)}...[/]")
        if from_utc and msg.date < from_utc:
            break
        if not msg.file:
            kind_counts["text/other"] = kind_counts.get("text/other", 0) + 1
            continue
        k = _kind(msg)
        kind_counts[k] = kind_counts.get(k, 0) + 1
        if types and not _matches(msg, types):
            continue
        results.append(msg)

    if verbose:
        console.print(f"[dim]total scanned: {scanned} | breakdown: {kind_counts}[/]")
    return results


async def _download_one(
    msg: Message,
    target: Path,
    progress: Progress,
    semaphore: asyncio.Semaphore,
    client: TelegramClient,
    fast: bool,
    connections: int,
    retries: int = 3,
) -> bool:
    """Download one message's media, retrying transient failures.

    Returns True on success (or skip), False if it failed after all retries.
    """
    filename = _filename(msg)
    path = target / filename
    expected = msg.file.size or 0
    if path.exists() and expected and path.stat().st_size == expected:
        progress.console.print(f"[dim]skip (exists): {filename}[/]")
        return True

    async with semaphore:
        label = filename if len(filename) <= 40 else filename[:37] + "..."
        task_id = progress.add_task(label, total=expected or None)

        def cb(current, total, task_id=task_id):
            progress.update(task_id, completed=current, total=total)

        for attempt in range(1, retries + 1):
            try:
                progress.update(task_id, completed=0)
                if fast and msg.document:
                    with open(path, "wb") as f:
                        await fast_download(client, msg, f, progress_callback=cb, connection_count=connections)
                else:
                    await client.download_media(msg, file=str(path), progress_callback=cb)
                # Verify the file landed at the expected size before declaring success
                if expected and path.exists() and path.stat().st_size != expected:
                    raise IOError(f"size mismatch: got {path.stat().st_size}, expected {expected}")
                progress.remove_task(task_id)
                return True
            except FloodWaitError as e:
                # Telegram explicitly told us how long to wait
                wait = e.seconds + 1
                progress.update(task_id, description=f"[yellow]flood wait {wait}s[/] {label}")
                await asyncio.sleep(wait)
            except Exception as e:
                if attempt < retries:
                    backoff = 2 ** attempt  # 2s, 4s, 8s...
                    progress.update(task_id, description=f"[yellow]retry {attempt}/{retries} in {backoff}s[/] {label}")
                    await asyncio.sleep(backoff)
                else:
                    progress.update(task_id, description=f"[red]failed[/] {label}")
                    progress.console.print(f"[red]Error downloading {filename} (gave up after {retries}): {e}[/]")
                    return False
        return False


async def download_chat(
    client: TelegramClient,
    chat: str | int,
    output: Path,
    types: list[str] | None = None,
    limit: int | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    message_ids: list[int] | None = None,
    fast: bool = True,
    connections: int | None = None,
    concurrency: int = 3,
    retries: int = 3,
) -> None:
    entity = await client.get_entity(chat)
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
    target = output / _safe(title)
    target.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold cyan]Chat:[/] {title}")
    console.print(f"[bold cyan]Output:[/] {target}")

    messages = await collect_messages(client, entity, types, limit, from_date, to_date, message_ids)
    if not messages:
        console.print("[yellow]No media matched the filters.[/]")
        return

    conn = connections or 4
    console.print(f"[bold green]Found {len(messages)} file(s)[/] — {concurrency} in parallel × up to {conn} conns each\n")

    semaphore = asyncio.Semaphore(concurrency)
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        results = await asyncio.gather(
            *[_download_one(msg, target, progress, semaphore, client, fast, conn, retries) for msg in messages]
        )

    ok = sum(1 for r in results if r)
    failed = [msg for msg, r in zip(messages, results) if not r]
    console.print(f"\n[bold green]Done:[/] {ok}/{len(messages)} succeeded")
    if failed:
        console.print(f"[bold red]Failed ({len(failed)}):[/]")
        ids = " ".join(f"-m {m.id}" for m in failed)
        console.print(f"  Re-run to retry just these:\n  python -m tgdl download {ids} -- {chat}")


async def list_media(
    client: TelegramClient,
    chat: str | int,
    types: list[str] | None = None,
    limit: int | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    verbose: bool = False,
) -> None:
    entity = await client.get_entity(chat)
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)
    console.print(f"[bold cyan]Chat:[/] {title}")

    messages = await collect_messages(client, entity, types, limit, from_date, to_date, None, verbose=verbose)
    if not messages:
        console.print("[yellow]No media matched the filters.[/]")
        return

    table = Table(title=f"{len(messages)} file(s)", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True, justify="right")
    table.add_column("Date", style="dim", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Size", justify="right")
    table.add_column("Dur.", justify="right", style="dim")
    table.add_column("Caption", style="green")

    total_size = 0
    for msg in messages:
        size = msg.file.size or 0
        total_size += size
        caption = (msg.message or "").replace("\n", " ")
        if len(caption) > 40:
            caption = caption[:37] + "..."
        table.add_row(
            str(msg.id),
            msg.date.strftime("%Y-%m-%d"),
            _kind(msg),
            _human_size(size),
            _human_duration(getattr(msg.file, "duration", None)),
            caption,
        )

    console.print(table)
    console.print(f"[bold]Total size:[/] {_human_size(total_size)}")
