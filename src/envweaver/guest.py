"""Compatibility entry point for existing ``python -m envweaver.guest`` images."""

from ._guest.server import main

if __name__ == "__main__":
    main()
