import sys
from pathlib import Path

# The repo-level scripts/ folder is not a package; make its modules importable in tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
