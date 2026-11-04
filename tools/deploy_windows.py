"""向授权的 Windows 专用项目目录同步源码，不包含运行状态或凭据。"""

from __future__ import annotations

import argparse
import base64
import subprocess
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.101.5")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.host.startswith("-") or any(c.isspace() for c in args.host):
        parser.error("SSH 目标无效")
    root = Path.cwd()
    archive = root / ".runtime/deploy/super-brain-windows.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in root.glob("*.md")]
    files += [root / name for name in ["pyproject.toml", "uv.lock"]]
    files += list(root.glob("config*.example.yaml"))
    for folder in ["src", "tests", "tools", ".agents", "frontend/src"]:
        files += [p for p in (root / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}]
    files += [root / "frontend" / name for name in ["package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "index.html"]]
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            if path.is_file():
                bundle.write(path, path.relative_to(root).as_posix())
    assert archive.stat().st_size < 5_000_000, "部署包意外过大，检查文件选择"
    subprocess.run(["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", str(archive), f"{args.host}:D:/Projects/super-brain-upload.zip"], capture_output=True, check=True)
    script = """
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
Expand-Archive -LiteralPath 'D:\\Projects\\super-brain-upload.zip' -DestinationPath 'D:\\Projects\\super-brain' -Force
Set-Location -LiteralPath 'D:\\Projects\\super-brain'
"""
    if args.install:
        script += "& uv sync --frozen --group dev --link-mode copy\nexit $LASTEXITCODE\n"
    else:
        script += "'Source sync completed.'\n"
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", args.host,
        "pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True, encoding="utf-8", errors="replace")
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
    print(f"已同步 {len(files)} 个文件，{archive.stat().st_size} 字节；运行状态与凭据未包含。")


if __name__ == "__main__":
    main()
