import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from .config import load_config
from .models import Result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="TicketPlus 查票與釋票監控工具")
    result.add_argument("--config", type=Path, help="YAML 設定檔；預設使用目前目錄的 config.yaml")
    sub = result.add_subparsers(dest="operation", required=True)
    for command in (
        "capabilities",
        "query",
        "status",
        "events",
        "check",
        "tick",
        "run",
        "health",
        "resume",
        "ui",
    ):
        p = sub.add_parser(command)
        # Also allow options after the operation, for AI/tool callers.
        p.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        p.add_argument("--json", action="store_true", help="輸出 JSON（預設已為 JSON）")
        if command == "ui":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8787)
            p.add_argument(
                "--public-origin",
                default=os.environ.get("TICKET_WATCHER_PUBLIC_ORIGIN"),
                help="Cloudflare Access 保護的 HTTPS origin，例如 https://tickets.example.com",
            )
        if command == "query":
            p.add_argument("--url", required=True)
            p.add_argument("--session-id", action="append", default=[])
            p.add_argument("--item-id", action="append", default=[])
        if command == "check":
            p.add_argument(
                "--now", action="store_true", help="略過例行排程，仍遵守平台限流與錯誤退避"
            )
        if command in {"status", "events", "check"}:
            p.add_argument("--target", required=command == "check")
        if command in {"query", "status", "events", "check"}:
            p.add_argument("--detail", choices=("summary", "full"), default="summary")
            p.add_argument("--limit", type=int, default=50)
            p.add_argument("--offset", type=int, default=0)
        if command == "resume":
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument("--target")
            group.add_argument("--platform", choices=("ticketplus",))
    return result


async def execute(args) -> Result:
    if args.operation == "capabilities":
        from .service import capabilities

        return capabilities()
    config_path = args.config
    if args.operation == "ui":
        from .web import serve

        await serve(
            config_path or Path("data/ui-config.yaml"),
            args.host,
            args.port,
            public_origin=args.public_origin,
        )
        return Result()
    if config_path is None and Path("config.yaml").is_file():
        config_path = Path("config.yaml")
    if config_path is None and args.operation in {"check", "tick", "run", "resume"}:
        raise ValueError("監控操作需提供 --config 或目前目錄的 config.yaml")
    if hasattr(args, "limit") and (not 1 <= args.limit <= 500 or args.offset < 0):
        raise ValueError("limit 必須介於 1 和 500，offset 不可小於 0")
    config = load_config(config_path)
    if args.operation == "health":
        from .health import read_health

        return read_health(config)
    from .service import Watcher

    async with Watcher(config) as watcher:
        paging = {"limit": getattr(args, "limit", 50), "offset": getattr(args, "offset", 0)}
        detail = getattr(args, "detail", "summary") == "full"
        match args.operation:
            case "query":
                return await watcher.query(
                    args.url,
                    session_ids=args.session_id,
                    item_ids=args.item_id,
                    detail=detail,
                    **paging,
                )
            case "status":
                return watcher.status(args.target, detail=detail, **paging)
            case "events":
                return watcher.events(args.target, detail=detail, **paging)
            case "check":
                return await watcher.check(args.target, detail=detail, immediate=args.now, **paging)
            case "tick":
                return await watcher.tick()
            case "resume":
                return watcher.resume(args.target, platform=bool(args.platform))
            case "run":
                if not any(t.enabled for t in config.targets):
                    raise ValueError("run 至少需要一個啟用的監控目標")
                await watcher.run()
    return Result()


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )
    # HTTPX INFO messages include complete URLs; keep webhook credentials out of logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        result = asyncio.run(execute(args))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, sqlite3.Error) as error:
        # Configuration errors may contain source input. File/YAML errors are summarized.
        safe = str(error) if isinstance(error, ValueError) else "檔案或資料庫操作失敗"
        result = Result("FAILED", data={"error": {"code": "CONFIG_OR_STORAGE", "message": safe}})
    except Exception:
        # Never emit raw HTTP exceptions or tracebacks with webhook tokens in JSON/logs.
        result = Result(
            "FAILED",
            data={
                "error": {"code": "INTERNAL_ERROR", "message": "程式執行異常，請檢查設定與測試結果"}
            },
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    if args.operation == "health" and not result.data.get("process_healthy"):
        return 1
    return {"COMPLETED": 0, "DEFERRED": 3, "UNSUPPORTED": 4, "FAILED": 2}.get(
        result.execution_status, 2
    )
