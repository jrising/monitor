import os
import sys
import tempfile
from pathlib import Path

# Configure the server before it is imported: throwaway data folder, a password, cron-style scheduling.
_tmp = tempfile.mkdtemp(prefix="monitor-test-")
os.environ.update({
    "MONITOR_DATA": _tmp,
    "MONITOR_PASSWORD": "test-password",
    "MONITOR_SCHEDULER": "external",
    "SECRET_TEST_KEY": "s3cret",
})
os.environ.pop("MONITOR_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
