import logging
import os
import sys

# Make the repo root importable so tests can `import metrics` / `import proxy`
# regardless of the directory pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The proxy logs per-request info and health-check errors. During tests (where
# a lingering daemon health-check thread deliberately hits dead backends) that
# would flood the output, so quiet everything below CRITICAL.
logging.disable(logging.ERROR)
