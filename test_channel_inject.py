#!/usr/bin/env python3
"""Test script for injecting a prompt into a running Claude Code session via the autoresume channel.

Prerequisites:
  - Claude Code must be running with the autoresume MCP server configured
  - The autoresume channel server must be listening (default port 18963)

Usage:
  python3 test_channel_inject.py                          # inject default resume prompt
  python3 test_channel_inject.py "say hello world"        # inject custom prompt
  python3 test_channel_inject.py --check                  # just check if channel is reachable
"""

import sys
import urllib.request

PORT = 18963


def check_channel() -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}",
            data=b"hello",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Channel not reachable on port {PORT}: {e}")
        return False


def inject(prompt: str) -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}",
            data=prompt.encode(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                print(f"Injected prompt into session: {prompt!r}")
                return True
            print(f"Unexpected status: {resp.status}")
            return False
    except Exception as e:
        print(f"Failed to inject: {e}")
        return False


def main():
    if "--check" in sys.argv:
        if check_channel():
            print(f"Channel is reachable on port {PORT}")
        else:
            sys.exit(1)
        return

    prompt = sys.argv[1] if len(sys.argv) > 1 else "hello"

    if not check_channel():
        print("Make sure Claude Code is running with the autoresume MCP server.")
        sys.exit(1)

    if not inject(prompt):
        sys.exit(1)


if __name__ == "__main__":
    main()
