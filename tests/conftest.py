import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LOYD_AUDIT_LOG", "off")    # tests opt in per-case
