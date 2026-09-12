import sys
from pathlib import Path

# Make wallwell.py importable from the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
