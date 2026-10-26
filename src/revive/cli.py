import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
from importlib.util import find_spec
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .engine import Engine
from .errors import ReviveError
from .paths import InstanceLock, check_plain_path, within
from .state import State


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Revive local photo processing with optional offline AI stages."
    )
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config")
    commands.add_parser("doctor")
    models = commands.add_parser(
        "models",
        help="Explicit model setup; normal processing never downloads weights.",
    )
    models.add_argument("action", choices=("status", "install"))
    models.add_argument(
        "--model",
        choices=(
            "ddcolor_tiny",
            "ddcolor_modelscope",
            "swinir_lightweight_x2",
            "realesrgan_x2plus",
        ),
        default=None,
    )
    run = commands.add_parser("run")
    run.add_argument(
        "--once",
        action="store_true",
        help="Settle/process current discoveries, then exit.",
    )
    run.add_argument("--launch-token", help=argparse.SUPPRESS)
    run.add_argument("--expected-config-identity", help=argparse.SUPPRESS)
    for name in ("start", "stop", "restart"):
        command = commands.add_parser(
            name, help=f"{name.capitalize()} the local background monitor."
        )
        command.add_argument("--timeout", type=float, default=20)
    startup = commands.add_parser(
        "startup", help="Export a reviewable Windows login task; does not register it."
    )
    startup.add_argument("action", choices=("export",))
    commands.add_parser("status")
    commands.add_parser("export-manifest")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id", type=int)
    args = parser.parse_args(argv)
    engine = None
    try:
        config = load_config(args.config)
        if args.command == "run" and args.expected_config_identity:
            from .runtime import identity

            if identity(config) != args.expected_config_identity:
                raise ReviveError(
                    "CONFIG_CHANGED",
                    "Configuration changed during launch; review it before starting again.",
                )
        if args.command in ("start", "stop", "restart"):
            from .runtime import control

            if not 0 < args.timeout <= 120:
                raise ReviveError(
                    "CONFIG_INVALID",
                    "Control timeout must be between 0 and 120 seconds.",
                )
            print(
                json.dumps(
                    control(config, args.config, args.command, args.timeout), indent=2
                )
            )
            return 0
        if args.command == "startup":
            from .startup import export_startup

            print(json.dumps(export_startup(config, args.config), indent=2))
            return 0
        if args.command == "validate-config":
            print(
                f"Configuration valid. Adapter: {config.adapter}; colorization enabled: {config.colorization_enabled}; super-resolution enabled: {config.sr_enabled}."
            )
            return 0
        if args.command == "models":
            from .models import install_model, model_status

            selected_model = args.model or config.colorization_model
            if args.action == "status":
                print(
                    json.dumps(model_status(config.app_data, selected_model), indent=2)
                )
            else:
                config.app_data.mkdir(parents=True, exist_ok=True)
                with InstanceLock(
                    within(config.app_data, config.app_data / "monitor.lock")
                ):
                    print(
                        json.dumps(
                            install_model(config.app_data, selected_model), indent=2
                        )
                    )
            return 0
        if args.command == "doctor":
            from importlib.metadata import version

            from PIL import features

            from .models import model_status

            asset = model_status(config.app_data, config.colorization_model)
            sr_asset = model_status(config.app_data, config.sr_model)
            sr_dependencies = all(
                find_spec(name) is not None for name in ("torch", "numpy")
            )
            dependencies = all(
                find_spec(name) is not None for name in ("torch", "numpy", "cv2")
            )

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
                        "effective_worker_commit_mib": config.worker_settings()[
                            "worker_commit_mib"
                        ],
                        "ai_available": bool(
                            (dependencies and asset["verified"])
                            or (sr_dependencies and sr_asset["verified"])
                        ),
                        "colorization_enabled": config.colorization_enabled,
                        "colorization_model": config.colorization_model,
                        "colorization_dependencies_installed": dependencies,
                        "model_verified": asset["verified"],
                        "super_resolution_enabled": config.sr_enabled,
                        "super_resolution_model": config.sr_model,
                        "super_resolution_dependencies_installed": sr_dependencies,
                        "super_resolution_model_verified": sr_asset["verified"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command in ("status", "export-manifest"):
            from .resources import memory_status
            from .runtime import monitor_status

            monitor = (
                monitor_status(config.app_data) if args.command == "status" else None
            )
            resources = memory_status(config.worker_memory_mib)
            effective = config.worker_settings()
            resources.update(
                {
                    key: effective[key]
                    for key in ("worker_memory_mib", "worker_commit_mib")
                }
            )
            database = within(config.app_data, config.app_data / "state.sqlite3")
            if not database.exists():
                if args.command == "status":
                    print(
                        json.dumps(
                            {
                                "counts": {},
                                "incidents": [],
                                "monitor": monitor,
                                "resources": resources,
                            }
                        )
                    )
                return 0
            state = State.readonly(database)
            try:
                if args.command == "status":
                    from .errors import recovery_action

                    attention = [
                        {**dict(row), "action": recovery_action(row["error_code"])}
                        for row in state.db.execute(
                            "SELECT id,state,error_code,attempts,next_attempt FROM jobs WHERE state IN ('failed','retry_pending') ORDER BY id DESC LIMIT 20"
                        )
                    ]
                    print(
                        json.dumps(
                            {
                                "counts": state.counts(),
                                "monitor": monitor,
                                "resources": resources,
                                "needs_attention": attention,
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
                    from .runtime import RuntimeSession

                    with RuntimeSession(
                        config,
                        args.config,
                        stop,
                        token=args.launch_token,
                        once=args.once,
                    ) as runtime:
                        engine.run(once=args.once, stop=stop, on_ready=runtime.ready)
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
