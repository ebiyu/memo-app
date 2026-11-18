import argparse
import logging
import os
from pathlib import Path


def default_data_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "memo"


def main() -> None:
    p = argparse.ArgumentParser(prog="memo-desktop")
    p.add_argument("--data-dir", type=Path, default=default_data_dir(),
                   help="設定と既定のメモ (notes/<slug>.txt) の置き場所")
    args = p.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.data_dir / "memo.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ウィンドウを作る前にやる必要がある (拡大表示でのぼけ防止)
    from .dpi import enable_dpi_awareness
    enable_dpi_awareness()

    from .app import MemoApp

    app = MemoApp(args.data_dir)
    app.MainLoop()


if __name__ == "__main__":
    main()
