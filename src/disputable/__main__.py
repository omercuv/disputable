"""python -m disputable: the same commands as the disputable console script."""

from __future__ import annotations


def run() -> int:
    from disputable.cli import main  # imported here, so importing the package never needs the CLI

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
