"""Standalone entry point; does not load or execute the UFI native patcher."""
from ufi_bridge.cli import main


if __name__ == '__main__':
    raise SystemExit(main())
