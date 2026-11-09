import argparse
import logging
import os
from pathlib import Path


def default_data_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "memo"


def main() -> None:
    p = argparse.ArgumentParser(prog="memo-desktop")
    p.add_argument("--data-dir", type=Path, default=default_data_dir())
    p.add_argument("--server", help="同期サーバーの URL (既定: config.json か http://127.0.0.1:8765)")
    p.add_argument("--interval", type=float, default=30.0, help="定期同期の間隔 (秒)")
    p.add_argument("--token", default=os.environ.get("MEMO_TOKEN"),
                   help="Bearer トークン (Authelia 連携までの暫定)")
    args = p.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.data_dir / "memo.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from .app import MemoApp

    app = MemoApp(args.data_dir, args.server, args.interval, args.token)
    app.MainLoop()


if __name__ == "__main__":
    main()
