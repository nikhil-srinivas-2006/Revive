import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .engine import Engine
from .errors import ReviveError
from .paths import InstanceLock, check_plain_path, within
from .state import State


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Revive local image normalization and automation (no AI models yet)."
    )
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config")
    commands.add_parser("doctor")
    run = commands.add_parser("run")
    run.add_argument(
        "--once",
        action="store_true",
        help="Settle/process current discoveries, then exit.",
    )
    commands.add_parser("status")
    commands.add_parser("export-manifest")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id", type=int)
    args = parser.parse_args(argv)
    engine = None
    try:
        config = load_config(args.config)
        if args.command == "validate-config":
            print(
                f"Configuration valid. Adapter: {config.adapter}; AI processing is unavailable."
            )
            return 0
        if args.command == "doctor":
            from importlib.metadata import version

            from PIL import features

            print(
                json.dumps(
                    {
                        "python": sys.version.split()[0],
                        "pillow": version("Pillow"),
                        "watchdog": version("watchdog"),
                        "littlecms": features.check("littlecms2"),
                        "input_exists": config.input.is_dir(),
                        "adapter": config.adapter,
                        "effective_worker_memory_mib": config.worker_settings()[
                            "worker_memory_mib"
                        ],
                        "ai_available": False,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command in ("status", "export-manifest"):
            database = within(config.app_data, config.app_data / "state.sqlite3")
            if not database.exists():
                if args.command == "status":
                    print(json.dumps({"counts": {}, "incidents": []}))
                return 0
            state = State.readonly(database)
            try:
                if args.command == "status":
                    print(
                        json.dumps(
                            {
                                "counts": state.counts(),
                                "incidents": [
                                    dict(row)
                                    for row in state.db.execute(
                                        "SELECT * FROM incidents"
                                    )
                                ],
                            },
                            indent=2,
                        )
                    )
                else:
                    for item in state.export():
                        print(json.dumps(item, ensure_ascii=False))
            finally:
                state.close()
            return 0
        config.app_data.mkdir(parents=True, exist_ok=True)
        with InstanceLock(within(config.app_data, config.app_data / "monitor.lock")):
            engine = Engine(config)
            if args.command == "retry":
                row = engine.state.db.execute(
                    "SELECT state FROM jobs WHERE id=?", (args.job_id,)
                ).fetchone()
                if not row or row["state"] not in ("failed", "retry_pending"):
                    raise ReviveError(
                        "JOB_NOT_RETRYABLE", "Choose a failed or retry-pending job."
                    )
                engine.state.update(
                    args.job_id,
                    state="queued",
                    attempts=0,
                    next_attempt=0,
                    error_code=None,
                )
            else:
                log = within(config.app_data, config.app_data / "revive.log")
                check_plain_path(log)
                handler = RotatingFileHandler(
                    log, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                logger = logging.getLogger("revive")
                logger.addHandler(handler)
                logger.setLevel(logging.WARNING)
                stop = threading.Event()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, lambda *_: stop.set())
                try:
                    engine.run(once=args.once, stop=stop)
                finally:
                    logger.removeHandler(handler)
                    handler.close()
        return 0
    except ReviveError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print(
            "IO_OR_STATE_ERROR: Local storage is unavailable or state is invalid.",
            file=sys.stderr,
        )
        return 2
    finally:
        if engine is not None:
            engine.close()
