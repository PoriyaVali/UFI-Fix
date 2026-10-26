import argparse
import json
import os
from pathlib import Path
import sys

from .core import BridgeError
from .protocol import serve
from .service import Bridge, tool_definitions
from .windows import discover_root
from . import __version__


def main():
    parser = argparse.ArgumentParser(description='Local UFI support API over MCP stdio (preview).')
    parser.add_argument('--version', action='version', version='UFI-Bridge ' + __version__)
    parser.add_argument('--ufi-dir', type=Path, help='Directory containing the genuine UFI.exe and its logs.')
    parser.add_argument('--state-dir', type=Path, help='Separate directory for original snapshots and edited copies.')
    parser.add_argument('--describe', action='store_true', help='Print schemas/capabilities without starting a session.')
    options = parser.parse_args()
    root = options.ufi_dir or discover_root()
    if root is None:
        print('UFI.exe was not found. Supply --ufi-dir.', file=sys.stderr)
        return 2
    state = options.state_dir or Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'UFI-Fix' / 'bridge'
    try:
        bridge = Bridge(root, state)
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if options.describe:
            print(json.dumps({'capabilities': bridge.capabilities(), 'tools': tool_definitions()}, indent=2))
            return 0
        return serve(bridge, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        bridge.close()
