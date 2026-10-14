"""检查 Git 暂存区，不显示发现的密钥内容。"""
from __future__ import annotations

import fnmatch
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DENIED = (
    ".env", ".env.*", "config.json", "style_examples.json", ".wechat_ai*",
    "logs/*", ".venv/*", ".publish-check/*", "venv/*", ".idea/*", ".vscode/*", "*/__pycache__/*",
    "__pycache__/*", "chat_exports/*", "private/*", "uploads/*", "*.pyc",
    "*.zip", "*.sqlite*", "*.db", "*.db-*", "*.log", "*.pem", "*.key",
)
PATTERNS = (
    ("API token", re.compile(rb"\bsk-[A-Za-z0-9_.-]{16,}")),
    ("GitHub token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("embedded API assignment", re.compile(rb"(?i)(?:api_key|access_token|client_secret)\s*=\s*[\"'][A-Za-z0-9_.-]{16,}[\"']")),
)


def known_secrets() -> list[bytes]:
    """只在本机读取 .env 中明确为密钥的字段，值不用于任何外部请求。"""
    path = ROOT / ".env"
    if not path.is_file():
        return []
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", key.strip(), re.I):
            value = value.strip().strip("\"'")
            if len(value) >= 8 and "在这里填写" not in value and value != "YOUR_API_KEY":
                values.append(value.encode("utf-8"))
    return values


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.PIPE)


def main() -> int:
    try:
        files = [item.decode("utf-8") for item in git("ls-files", "-z").split(b"\0") if item]
        if not files:
            print("未发现暂存或已跟踪文件，请先暂存准备公开的文件。")
            return 2
        failures = []
        secrets = known_secrets()
        for name in files:
            if name != ".env.example" and any(fnmatch.fnmatchcase(name, pattern) for pattern in DENIED):
                failures.append((name, "private file"))
                continue
            data = git("show", ":" + name)
            for label, pattern in PATTERNS:
                if pattern.search(data):
                    failures.append((name, label))
            if any(value in data for value in secrets):
                failures.append((name, "local .env secret"))
        if failures:
            for name, label in failures:
                print(f"禁止公开：{name} ({label})")
            return 1
        print(f"公开文件检查通过：{len(files)} 个文件；未发现禁止公开文件或匹配的密钥。")
        return 0
    except subprocess.CalledProcessError:
        print("无法读取 Git 暂存区，请确认项目已初始化为 Git 仓库。")
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
