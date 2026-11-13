"""Double-click source launcher; the packaged exe does not require Python."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from tensor_desktop import main

main()
