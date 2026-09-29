#!/usr/bin/env python3
"""为空的 summary.json 补跑摘要（一次性脚本，需要真实 API Key）。

背景：思考型模型（deepseek-flash）的思考内容计入 max_tokens，
2026-09 之前 summarize() 额度写死 1024，导致多份 summary.json 为 `{}`。
client.py 已修复提额重试，本脚本对存量空摘要补跑一次。

用法（在有 data/ 数据和真实 Key 的机器上）：
    python scripts/backfill_summaries.py --dry-run
    python scripts/backfill_summaries.py --api-key sk-xxx
    python scripts/backfill_summaries.py --api-key sk-xxx --model deepseek-chat

Key 也可用环境变量 V2L_LLM_API_KEY（或 DEEPSEEK_API_KEY）传入；
模型/接入点默认取 config.yaml 的 llm.model / llm.base_url，可用
--model / --base-url 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _is_empty_summary(path: Path) -> bool:
    """`{}`、解析失败、或 summary 为空字符串都视为需要补跑。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(data, dict):
        return True
    return not (data.get("summary") or "").strip()


def find_empty_summaries(data_dir: Path) -> list[Path]:
    return sorted(
        path for path in data_dir.glob("*/variants/*/summary.json")
        if _is_empty_summary(path)
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为空的 summary.json 补跑摘要",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法", 1)[1] if __doc__ else None,
    )
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("V2L_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"),
    )
    parser.add_argument("--base-url", default=None, help="默认取 config.yaml 的 llm.base_url")
    parser.add_argument("--model", default=None, help="默认取 config.yaml 的 llm.model")
    parser.add_argument("--dry-run", action="store_true", help="只列出将补跑的摘要，不调用 API")
    args = parser.parse_args()

    empty = find_empty_summaries(args.data_dir)
    if not empty:
        print("没有需要补跑的空摘要")
        return 0

    print(f"发现 {len(empty)} 份空摘要:")
    for path in empty:
        print(f"  {path}")

    if args.dry_run:
        return 0

    if not args.api_key:
        print("错误: 缺少 API Key，用 --api-key 或环境变量 V2L_LLM_API_KEY 提供", file=sys.stderr)
        return 1

    llm_config: dict = {"api_key": args.api_key}
    if args.base_url:
        llm_config["base_url"] = args.base_url
    if args.model:
        llm_config["model"] = args.model

    from src.translation.client import summarize

    ok = 0
    for summary_path in empty:
        variant_dir = summary_path.parent
        video_id = variant_dir.parent.parent.name
        mode = variant_dir.name
        script_path = variant_dir / "script_zh.txt"
        if not script_path.exists():
            print(f"[跳过] {video_id}/{mode}: 缺少 script_zh.txt", file=sys.stderr)
            continue

        metadata: dict = {}
        meta_path = args.data_dir / video_id / "metadata.json"
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}

        print(f"[补跑] {video_id}/{mode} ...", flush=True)
        try:
            summary = summarize(
                script_path.read_text(encoding="utf-8"), metadata, llm_config=llm_config,
            )
        except Exception as e:
            print(f"[失败] {video_id}/{mode}: {e}", file=sys.stderr)
            continue

        if not isinstance(summary, dict) or not (summary.get("summary") or "").strip():
            print(f"[失败] {video_id}/{mode}: 摘要仍为空", file=sys.stderr)
            continue

        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        ok += 1
        print(f"[完成] {summary_path}")

    print(f"补跑完成: {ok}/{len(empty)}")
    return 0 if ok == len(empty) else 1


if __name__ == "__main__":
    raise SystemExit(main())
