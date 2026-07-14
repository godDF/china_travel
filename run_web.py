#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
启动 ChinaTravel Web 对话应用

Usage:
    python run_web.py

然后在浏览器打开 http://localhost:8000
"""

import sys
import os
from dotenv import load_dotenv

# Keep emoji and Chinese startup logs working in Windows terminals/background processes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load project-local configuration before importing the backend or models.
load_dotenv(os.path.join(project_root, ".env"))

if __name__ == "__main__":
    # Check environment
    if not os.environ.get("OPENAI_API_KEY"):
        print("⚠️  警告: 未设置 OPENAI_API_KEY 环境变量")
        print("   请在 PowerShell 中执行:")
        print('   $env:OPENAI_API_KEY = "your-key"')
        print()
        print("   或设置 SSL_CERT_FILE:")
        print('   $env:SSL_CERT_FILE = "path/to/certifi/cacert.pem"')
        print()

    print("🚀 启动 ChinaTravel Web 服务...")
    print("📍 浏览器打开: http://localhost:8000")
    print("📋 按 Ctrl+C 停止服务")
    print()

    import uvicorn
    from backend.main import app
    uvicorn.run(app, host="0.0.0.0", port=8000)
