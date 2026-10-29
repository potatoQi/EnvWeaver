"""Start the optional management API and its static frontend."""

import argparse


def main():
    parser = argparse.ArgumentParser(description="EnvWeaver asset management")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    try:
        import uvicorn

        from .api import create_app
    except ImportError as error:
        raise SystemExit(
            'Install management dependencies: pip install -e ".[management]"'
        ) from error
    print(f"Management: http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
