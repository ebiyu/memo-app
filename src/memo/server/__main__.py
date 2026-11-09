import argparse
import os

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(prog="memo-server")
    p.add_argument("--host", default=os.environ.get("MEMO_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MEMO_PORT", "8765")))
    p.add_argument("--db", default=os.environ.get("MEMO_DB", "data/memo.db"))
    args = p.parse_args()

    from .app import create_app

    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
