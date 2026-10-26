"""Command line: `hearthkeep serve | enroll-code | secrets set/list`."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from hearthkeep import db
from hearthkeep.api.app import DB_FILE
from hearthkeep.api.common import ENROLLMENT_CODE_TTL_MINUTES
from hearthkeep.auth.enrollment import EnrollmentCodes, EnrollmentError, first_device_allowed
from hearthkeep.config import ConfigError, Settings, init_data_dir, load_settings
from hearthkeep.secrets import SecretStore, SecretStoreError

EXIT_USAGE = 2


def _settings(data: Path) -> Settings:
    init_data_dir(data)
    return load_settings(data)


def _connect(settings: Settings):
    return db.connect(Path(settings.data_dir) / DB_FILE)


def _print_code(code: str) -> None:
    print(
        f"\nFirst-device enrollment code: {code}\n"
        f"Open Hearthkeep in your browser and enter it within {ENROLLMENT_CODE_TTL_MINUTES} "
        "minutes to register your first passkey.\n"
    )


def build_registry():
    from hearthkeep.tools.base import ToolRegistry
    from hearthkeep.tools.demo import ClockTool, NoteTool

    registry = ToolRegistry()
    registry.register(ClockTool())
    registry.register(NoteTool())
    return registry


def run_server(app, host: str, port: int) -> None:  # replaced in tests
    import uvicorn

    # proxy_headers=False: never derive the client address from X-Forwarded-For, so a local
    # process cannot rotate the rate-limit key by spoofing that header.
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)


def cmd_serve(args: argparse.Namespace) -> int:
    from hearthkeep.api.app import create_app
    from hearthkeep.auth.insecure_dev import (
        WARNING,
        InsecureDevAuthError,
        InsecureDevVerifier,
        check_allowed,
    )
    from hearthkeep.auth.webauthn import PyWebAuthnVerifier
    from hearthkeep.providers.chat import OpenAIChatClient

    settings = _settings(Path(args.data))
    if args.insecure_dev_auth:
        conn = _connect(settings)
        try:
            check_allowed(settings, conn)
        except InsecureDevAuthError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        finally:
            conn.close()
        print(WARNING, file=sys.stderr)
        verifier = InsecureDevVerifier()
    else:
        verifier = PyWebAuthnVerifier(settings)

    secrets = SecretStore(settings.data_dir)
    api_key = secrets.get(settings.chat_api_key_secret) or ""
    chat_client = OpenAIChatClient(settings.chat_base_url, api_key, settings.chat_model)

    conn = _connect(settings)
    try:
        if first_device_allowed(conn):
            _print_code(EnrollmentCodes(conn).issue("first_device", ENROLLMENT_CODE_TTL_MINUTES))
    finally:
        conn.close()

    app = create_app(
        settings,
        verifier=verifier,
        chat_client=chat_client,
        registry=build_registry(),
        secrets=secrets,
    )
    # Never bind anything but the loopback interface.
    host = "127.0.0.1" if settings.host == "localhost" else settings.host
    run_server(app, host, settings.port)
    return 0


def cmd_enroll_code(args: argparse.Namespace) -> int:
    settings = _settings(Path(args.data))
    conn = _connect(settings)
    try:
        code = EnrollmentCodes(conn).issue("first_device", ENROLLMENT_CODE_TTL_MINUTES)
    except EnrollmentError:
        print(
            "error: a passkey is already enrolled. Add more devices from Settings > Devices "
            "in the web app.",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()
    _print_code(code)
    return 0


def _read_secret_value(name: str) -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(f"Value for '{name}' (input hidden): ")
    return sys.stdin.read().rstrip("\r\n")


def cmd_secrets_set(args: argparse.Namespace) -> int:
    settings = _settings(Path(args.data))
    store = SecretStore(settings.data_dir)
    value = _read_secret_value(args.name)
    if not value:
        print("error: empty value, nothing stored", file=sys.stderr)
        return 1
    try:
        store.put(args.name, value)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"Stored secret '{args.name}'.")
    return 0


def cmd_secrets_list(args: argparse.Namespace) -> int:
    settings = _settings(Path(args.data))
    for name in SecretStore(settings.data_dir).names():
        print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hearthkeep", description="Your private AI, at home.")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_data(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--data", required=True, help="path to your Hearthkeep data folder")
        return p

    serve = with_data(sub.add_parser("serve", help="run the Hearthkeep server"))
    serve.add_argument(
        "--insecure-dev-auth",
        action="store_true",
        help="DEVELOPMENT ONLY: accept any passkey response (localhost origins only)",
    )
    serve.set_defaults(func=cmd_serve)

    enroll = with_data(sub.add_parser("enroll-code", help="issue a first-device enrollment code"))
    enroll.set_defaults(func=cmd_enroll_code)

    secrets = sub.add_parser("secrets", help="manage encrypted secrets")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    put = with_data(secrets_sub.add_parser("set", help="store a secret (value is never echoed)"))
    put.add_argument("name")
    put.set_defaults(func=cmd_secrets_set)
    names = with_data(secrets_sub.add_parser("list", help="list secret names (not values)"))
    names.set_defaults(func=cmd_secrets_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SecretStoreError as exc:
        print(f"secret store error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
