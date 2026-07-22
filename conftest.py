import os
import sys

# 让 `from sessions.zh.sNN import ...` 在 pytest 下可用。
sys.path.insert(0, os.path.dirname(__file__))
