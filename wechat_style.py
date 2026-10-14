"""从用户提供的微信文本提取本人风格；不上传原始聊天或训练模型权重。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

STYLE_PATH = Path(__file__).resolve().parent / "style_examples.json"
TIMESTAMP = re.compile(r"(?m)^\s*(\d{4})年(\d{2})月(\d{2})日\s+(\d{1,2}):(\d{2})[^\S\r\n]*$")
PRIVATE = re.compile(r"https?://|\d{5,}|密码|验证码|银行卡|身份证|住址|转账|付款|支付|账号|账户|@|借我|我给你钱", re.I)
TOKEN_SECRET = re.compile(r"(?<![\w])(?=[A-Za-z0-9_,.!-]{8,}(?![\w]))(?=[A-Za-z0-9_,.!-]*[A-Za-z])(?=[A-Za-z0-9_,.!-]*\d)[A-Za-z0-9_,.!-]+")


def clean_sample(text: str, maximum: int = 180) -> str | None:
    text = " ".join(text.split()).strip()
    if not text or len(text) > maximum or PRIVATE.search(text) or TOKEN_SECRET.search(text):
        return None
    if text in {"[图片]", "[动画表情]", "[视频]", "[语音]"}:
        return None
    return text


def parse_chat(text: str, owner: str) -> list[dict[str, Any]]:
    """以日期行定位消息；兼容粘贴时正文与下一位说话人粘连。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    dates = list(TIMESTAMP.finditer(text))
    preceding = []
    for date in dates:
        end = date.start()
        # 时间行前的名字独占一行，部分导出片段会与上一条正文连在一起。
        prefix = text[:end].rstrip("\n")
        start = prefix.rfind("\n") + 1
        preceding.append((start, prefix[start:].strip()))
    counts = Counter(name for _, name in preceding)
    names = {owner} | {name for name in counts
                       if 0 < len(name) <= 32 and not re.search(r"\s", name)}
    headers = []
    for date, (start, line) in zip(dates, preceding):
        # 优先选最短的已知名字，避免把粘连的正文误识别成名字。
        matches = sorted((name for name in names if line.endswith(name)), key=len)
        if not matches:
            continue
        sender = owner if line.endswith(owner) else matches[0]
        sender_start = start + text[start:date.start()].rfind(sender)
        stamp = datetime(*(int(value) for value in date.groups()))
        headers.append((sender_start, date.end(), sender, stamp))
    rows, seen = [], set()
    for index, (_, end, sender, stamp) in enumerate(headers):
        stop = headers[index + 1][0] if index + 1 < len(headers) else len(text)
        body = text[end:stop].strip()
        # 附件内嵌的请求不属于聊天正文。
        body = re.split(r"帮我把这个微信聊天记录训练到", body, maxsplit=1)[0].strip()
        key = (sender, stamp, body)
        if body and key not in seen:
            rows.append({"sender": sender, "time": stamp, "text": body})
            seen.add(key)
    return rows


def make_profile(text: str, owner: str) -> dict[str, Any]:
    rows = parse_chat(text, owner)
    mine = [sample for row in rows if row["sender"] == owner
            if (sample := clean_sample(row["text"], 120)) is not None]
    if not mine:
        raise ValueError(f"没有找到 {owner} 的可用文字消息")
    frequencies = Counter(mine)
    unique = list(dict.fromkeys(mine))
    exchanges = []
    for index, row in enumerate(rows):
        if row["sender"] != owner or index == 0 or rows[index - 1]["sender"] == owner:
            continue
        other = rows[index - 1]["sender"]
        before = []
        for previous in reversed(rows[:index]):
            if previous["sender"] != other or not 0 <= (row["time"] - previous["time"]).total_seconds() <= 600:
                break
            before.insert(0, previous["text"])
            if len(before) == 4:
                break
        after = []
        for following in rows[index:]:
            if following["sender"] != owner or not 0 <= (following["time"] - row["time"]).total_seconds() <= 600:
                break
            after.append(following["text"])
            if len(after) == 4:
                break
        context, reply = clean_sample(" ".join(before)), clean_sample(" ".join(after), 120)
        if context and reply and (context, reply) not in {(item["context"], item["reply"]) for item in exchanges}:
            exchanges.append({"context": context, "reply": reply})
    lengths = sorted(len(sample) for sample in mine)
    return {
        "version": 2, "owner": owner, "mine": unique, "exchanges": exchanges,
        "frequent": [sample for sample, _ in frequencies.most_common(16)],
        "statistics": {"parsed_messages": len(rows), "owner_messages": sum(row["sender"] == owner for row in rows),
                       "usable_owner_messages": len(mine), "unique_samples": len(unique),
                       "exchanges": len(exchanges), "median_chars": lengths[len(lengths) // 2]},
    }


def load_profile(path: Path = STYLE_PATH) -> dict[str, Any]:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]{2,}", text.lower()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(part[index:index + 2] for index in range(len(part) - 1))
    return words - {"你的", "我的", "我是", "你是", "什么", "怎么", "这个", "那个"}


def style_reference(profile: dict[str, Any], query: str = "", include_samples: bool = True) -> str:
    """只选少量代表样本和相关问答，控制每次请求的长度。"""
    mine = [sample for value in profile.get("mine", []) if isinstance(value, str)
            if (sample := clean_sample(value, 120)) is not None]
    if not mine:
        return "（尚未导入）"
    frequent = [value for value in profile.get("frequent", []) if value in mine]
    representative = list(dict.fromkeys(frequent + mine))[:20]
    terms = _tokens(query)
    pairs = [item for item in profile.get("exchanges", []) if isinstance(item, dict)
             and isinstance(item.get("context"), str) and isinstance(item.get("reply"), str)
             and clean_sample(item["context"]) and clean_sample(item["reply"], 120)]
    def relevance(item: dict[str, str]) -> float:
        tokens = _tokens(item["context"])
        return len(terms & tokens) / max(1, len(tokens)) ** 0.5

    related = sorted(pairs, key=relevance, reverse=True)
    best = relevance(related[0]) if related else 0
    selected = [item for item in related if relevance(item) > 0 and relevance(item) >= best * 0.6][:3]
    # 与真实会话历史分开，用 JSON 表示资料中的消息。
    reference = {"历史对话示例": selected}
    if include_samples:
        reference["本人用语"] = representative
    return json.dumps(reference, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="导入粘贴或导出的微信文本作为本人风格参考")
    parser.add_argument("path", type=Path)
    parser.add_argument("--owner", required=True, help="你在导出记录中的精确名字")
    args = parser.parse_args()
    try:
        profile = make_profile(args.path.read_text(encoding="utf-8-sig"), args.owner)
        STYLE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"导入失败：{exc}")
        return 2
    stats = profile["statistics"]
    print(f"已导入 {stats['unique_samples']} 条本人用语、{stats['exchanges']} 组历史回复示例。重启助手后生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
