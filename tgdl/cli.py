import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from tgdl.client import telegram_client
from tgdl.config import Config
from tgdl.downloader import download_chat, list_media

app = typer.Typer(help="Telegram media downloader — descarga medios de chats/canales via MTProto")
console = Console()

VALID_TYPES = {"photo", "video", "audio", "voice", "document"}


def _parse_chat(raw: str) -> str | int:
    if raw == "me":
        return "me"
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError as e:
        raise typer.BadParameter(f"Invalid date '{s}'. Use YYYY-MM-DD or ISO 8601.") from e


def _validate_types(values: Optional[list[str]]) -> Optional[list[str]]:
    if not values:
        return None
    bad = [v for v in values if v not in VALID_TYPES]
    if bad:
        raise typer.BadParameter(
            f"Unknown type(s): {', '.join(bad)}. Valid: {', '.join(sorted(VALID_TYPES))}"
        )
    return values


@app.command()
def login():
    """Log in to Telegram (interactive first time) and save session."""
    async def _run():
        async with telegram_client() as client:
            me = await client.get_me()
            handle = f"@{me.username}" if me.username else str(me.id)
            console.print(f"[green]Logged in as:[/] {me.first_name} ({handle})")
    asyncio.run(_run())


@app.command()
def whoami():
    """Show current account info, including Premium status."""
    async def _run():
        async with telegram_client() as client:
            me = await client.get_me()
            premium = getattr(me, "premium", False)
            tag = "[bold magenta]PREMIUM[/]" if premium else "[dim]free[/]"
            handle = f"@{me.username}" if me.username else "(no username)"
            console.print(f"[bold]ID:[/]       {me.id}")
            console.print(f"[bold]Name:[/]     {me.first_name or ''} {me.last_name or ''}".rstrip())
            console.print(f"[bold]Handle:[/]   {handle}")
            console.print(f"[bold]Phone:[/]    +{me.phone}" if me.phone else "[bold]Phone:[/]    -")
            console.print(f"[bold]Status:[/]   {tag}")
            if not premium:
                console.print("\n[yellow]Si acabas de pagar Premium, borra tgdl.session y re-login:[/]")
                console.print("  rm tgdl.session && python -m tgdl login")
    asyncio.run(_run())


@app.command()
def chats(limit: int = typer.Option(50, help="Max dialogs to list")):
    """List your chats with their IDs."""
    async def _run():
        async with telegram_client() as client:
            table = Table(title="Your chats", show_lines=False)
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Kind", style="magenta")
            table.add_column("Name")
            table.add_column("Username", style="green")
            async for dialog in client.iter_dialogs(limit=limit):
                entity = dialog.entity
                kind = type(entity).__name__
                username = f"@{entity.username}" if getattr(entity, "username", None) else ""
                table.add_row(str(dialog.id), kind, dialog.name or "(no name)", username)
            console.print(table)
    asyncio.run(_run())


@app.command()
def info(chat: str = typer.Argument(..., help="@username, numeric ID, or 'me'")):
    """Diagnostic: show entity details + try to fetch 1 message."""
    async def _run():
        async with telegram_client() as client:
            entity = await client.get_entity(_parse_chat(chat))
            console.print(f"[bold]Entity type:[/] {type(entity).__name__}")
            for attr in ("id", "title", "username", "megagroup", "broadcast",
                         "gigagroup", "restricted", "noforwards", "has_link",
                         "left", "participants_count", "access_hash"):
                if hasattr(entity, attr):
                    console.print(f"  {attr}: {getattr(entity, attr)}")
            reasons = getattr(entity, "restriction_reason", None)
            if reasons:
                console.print("[bold red]Restriction reasons:[/]")
                for r in reasons:
                    console.print(f"  - platform={r.platform} reason={r.reason} text={r.text!r}")

            console.print("\n[bold]Trying to fetch messages...[/]")
            count = 0
            async for msg in client.iter_messages(entity, limit=5):
                count += 1
                has_file = "FILE" if msg.file else "text"
                preview = (msg.message or "")[:50].replace("\n", " ")
                console.print(f"  msg {msg.id} [{has_file}] {msg.date} — {preview!r}")
            console.print(f"[bold]Got {count} message(s)[/]")

            if count == 0:
                console.print("\n[yellow]Zero messages returned. Common causes:[/]")
                console.print("  - Group has 'Hidden history for new members' enabled")
                console.print("  - You don't have permission to read message history")
                console.print("  - Channel is restricted or your account can't view it")
    asyncio.run(_run())


@app.command()
def verify(
    chat: str = typer.Argument(..., help="@username, numeric ID, or 'me'"),
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    media_type: Optional[list[str]] = typer.Option(None, "--type", "-t"),
):
    """Compare chat media against files on disk; report missing IDs."""
    from tgdl.downloader import collect_messages, _filename, _safe

    types = _validate_types(media_type)
    base = output or Config.load().output_dir

    async def _run():
        async with telegram_client() as client:
            entity = await client.get_entity(_parse_chat(chat))
            title = getattr(entity, "title", None) or str(entity.id)
            target = base / _safe(title)
            messages = await collect_messages(client, entity, types, None, None, None, None)

            console.print(f"[bold cyan]Chat:[/] {title} — {len(messages)} files in chat")
            console.print(f"[bold cyan]Disk:[/]  {target}")

            if not target.exists():
                console.print(f"[red]Output dir doesn't exist:[/] {target}")
                return

            missing = []
            partial = []
            ok = 0
            for msg in messages:
                if not msg.file:
                    continue
                path = target / _filename(msg)
                expected = msg.file.size or 0
                if not path.exists():
                    missing.append((msg.id, expected))
                elif expected and path.stat().st_size != expected:
                    partial.append((msg.id, path.stat().st_size, expected))
                else:
                    ok += 1

            console.print(f"[green]Complete:[/] {ok}")
            if partial:
                console.print(f"[yellow]Partial ({len(partial)}):[/]")
                for mid, got, exp in partial:
                    console.print(f"  msg {mid}: {got} bytes / {exp} expected")
            if missing:
                console.print(f"[red]Missing ({len(missing)}):[/]")
                for mid, exp in missing:
                    console.print(f"  msg {mid}: {exp} bytes expected")
                ids = " ".join(f"-m {mid}" for mid, _ in missing)
                console.print(f"\n[bold]Re-download missing:[/]\n  python -m tgdl download {ids} -- {chat}")
    asyncio.run(_run())


@app.command("list-media")
def list_media_cmd(
    chat: str = typer.Argument(..., help="@username, numeric ID (for negative IDs use: -- -1001234567890), or 'me'"),
    media_type: Optional[list[str]] = typer.Option(
        None, "--type", "-t",
        help="Filter by type (repeatable): photo, video, audio, voice, document",
    ),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max messages to scan"),
    from_date: Optional[str] = typer.Option(None, "--from", help="Only messages after YYYY-MM-DD"),
    to_date: Optional[str] = typer.Option(None, "--to", help="Only messages before YYYY-MM-DD"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show scan progress + message-type breakdown"),
):
    """Preview media in a chat without downloading."""
    types = _validate_types(media_type)
    from_dt = _parse_date(from_date)
    to_dt = _parse_date(to_date)

    async def _run():
        async with telegram_client() as client:
            await list_media(
                client=client,
                chat=_parse_chat(chat),
                types=types,
                limit=limit,
                from_date=from_dt,
                to_date=to_dt,
                verbose=verbose,
            )
    asyncio.run(_run())


@app.command()
def download(
    chat: str = typer.Argument(..., help="@username, numeric ID (for negative IDs use: -- -1001234567890), or 'me'"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory (defaults to TG_OUTPUT_DIR from .env)"),
    media_type: Optional[list[str]] = typer.Option(
        None, "--type", "-t",
        help="Filter by type (repeatable): photo, video, audio, voice, document",
    ),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max messages to scan"),
    from_date: Optional[str] = typer.Option(None, "--from", help="Only messages after YYYY-MM-DD"),
    to_date: Optional[str] = typer.Option(None, "--to", help="Only messages before YYYY-MM-DD"),
    message_id: Optional[list[int]] = typer.Option(
        None, "--message-id", "-m", help="Specific message ID (repeatable)"
    ),
    fast: bool = typer.Option(True, "--fast/--no-fast", help="Use parallel MTProto connections (much faster)"),
    connections: Optional[int] = typer.Option(None, "--connections", "-c", help="Max parallel conns per file (default: 4, adaptive by file size)"),
    concurrency: int = typer.Option(3, "--concurrency", "-C", help="How many files to download in parallel (default: 3)"),
    retries: int = typer.Option(3, "--retries", "-r", help="Retry attempts per file on transient failures (default: 3)"),
):
    """Download media from a chat."""
    types = _validate_types(media_type)
    target = output or Config.load().output_dir
    from_dt = _parse_date(from_date)
    to_dt = _parse_date(to_date)

    async def _run():
        async with telegram_client() as client:
            await download_chat(
                client=client,
                chat=_parse_chat(chat),
                output=target,
                types=types,
                limit=limit,
                from_date=from_dt,
                to_date=to_dt,
                message_ids=message_id or None,
                fast=fast,
                connections=connections,
                concurrency=concurrency,
                retries=retries,
            )
    asyncio.run(_run())


if __name__ == "__main__":
    app()
