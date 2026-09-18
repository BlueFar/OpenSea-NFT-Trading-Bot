#!/usr/bin/env python3
"""
Dashboard launcher for the 24/7 OpenSea NFT Monitoring Bot.
Runs a local web server providing Start/Stop controls, status metrics,
and interactive 7-condition collection inspection.
"""

import sys
import argparse
from src.dashboard.server import run_dashboard_server

def main():
    parser = argparse.ArgumentParser(description="NFT Bot Local Web Dashboard")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5050, help="Port to listen on (default: 5050)")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to config file")

    args = parser.parse_args()
    run_dashboard_server(host=args.host, port=args.port, config_path=args.config)

if __name__ == "__main__":
    main()
