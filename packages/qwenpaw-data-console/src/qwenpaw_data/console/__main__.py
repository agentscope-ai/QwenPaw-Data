"""Run a loopback portal against existing Host and DataBridge services."""

import argparse
import json
import os
from pathlib import Path

from . import ConsoleConfig, create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-url", default="http://127.0.0.1:8766")
    parser.add_argument("--bridge-url", help="Optional local DataBridge API URL")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--config", type=Path, help="Public ConsoleConfig JSON; no credentials"
    )
    args = parser.parse_args()
    token = os.environ.get("QWENPAW_DATA_API_TOKEN", "")
    if not token:
        parser.error("Set QWENPAW_DATA_API_TOKEN for the Host service")
    config = (
        ConsoleConfig.model_validate(json.loads(args.config.read_text()))
        if args.config
        else ConsoleConfig()
    )
    app = create_app(
        host_url=args.host_url,
        token=token,
        bridge_url=args.bridge_url,
        bridge_token=os.environ.get("QWENPAW_DATA_CONTEXT_API_TOKEN") or token,
        config=config,
    )
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
