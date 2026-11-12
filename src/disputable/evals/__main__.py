"""python -m disputable.evals ARGS: the same as disputable eval ARGS."""

import sys

from disputable.evals import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
