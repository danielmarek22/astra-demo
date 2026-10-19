"""Launch one independent, local demo process."""

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Astra standalone demo")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--config",
        help="Optional demo YAML; defaults to local config or checked-in template",
    )
    args = parser.parse_args(argv)
    import uvicorn
    from app.config import Config
    from app.server import create_app

    uvicorn.run(
        create_app(Config(args.config)), host="127.0.0.1", port=args.port, workers=1
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
