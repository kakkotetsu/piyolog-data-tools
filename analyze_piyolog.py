#!/usr/bin/env python3
"""Create a private parenting review from archived feeds without modifying them."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

from piyolog.advisor.config import load_config, load_llm_settings
from piyolog.advisor.llm import build_request_body, generate_advice
from piyolog.advisor.metrics import build_analysis
from piyolog.advisor.report import render_report


PROJECT_DIR = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 512 * 1024

# A small, explicit reference set; the model must not invent additional sources.
# This is a population guideline, not a diagnosis or an individual sleep target.
REFERENCE_GUIDANCE = [
    {
        "title": "AASM Child Sleep Duration Health Advisory",
        "url": "https://aasm.org/advocacy/position-statements/child-sleep-duration-health-advisory/",
        "statement": "Infants 4 months to 12 months should sleep 12 to 16 hours per 24 hours (including naps).",
        "scope": "一般的な目安。個人差・記録漏れを考慮し、記録だけで睡眠不足と診断しない。",
        "verified_on": "2026-09-28",
    }
]


def profile_at(child, as_of: date) -> dict:
    """Send completed months, never the birth date or exact age in days."""
    def months(birthday: date) -> int:
        if birthday > as_of:
            raise RuntimeError("分析期間の最終日より生年月日・修正月齢の基準日が後になっています。")
        return (as_of.year - birthday.year) * 12 + as_of.month - birthday.month - (as_of.day < birthday.day)

    profile = {"age_months": months(child.birth_date), "age_as_of": as_of.isoformat()}
    if child.corrected_birth_date is not None:
        profile["corrected_age_months"] = months(child.corrected_birth_date)
    return profile


def private_write(path: Path, text: str) -> None:
    """Publish an owner-only file only after writing succeeds; never overwrite."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        # Both names are in the same directory. A hard link publishes atomically
        # and fails if path already exists, unlike replace/rename-overwrite.
        os.link(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except OSError:
            # Failure to remove a private temporary file must not unpublish a
            # complete report or mask the original write error.
            pass


def safe_output_dir(output_dir: Path, data_dir: Path) -> Path:
    output = output_dir.resolve()
    for archive in (data_dir.resolve(), (PROJECT_DIR / "data" / "piyolog").resolve()):
        if output == archive or archive in output.parents:
            raise RuntimeError("出力先に原本ディレクトリまたはその配下は指定できません。")
    return output


def apply_proxy_settings(env_file: Path) -> None:
    """Use optional .env proxy settings without executing it or replacing OS settings."""
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"} and value:
            if key not in os.environ and key.lower() not in os.environ:
                os.environ[key.lower()] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ぴよログの集計とAIの提言をローカルMarkdownに保存します。")
    parser.add_argument("--config", type=Path, default=PROJECT_DIR / "advisor.toml")
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR / "data" / "piyolog")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_DIR / "data" / "advisor" / "reports")
    parser.add_argument("--days", type=int, help="分析日数（1〜90日）。設定ファイルより優先します。")
    parser.add_argument("--end-date", type=date.fromisoformat, help="排他的な終了日 YYYY-MM-DD。この日の0時までを分析します。")
    parser.add_argument("--dry-run", action="store_true", help="外部通信せず、送信予定JSONと集計Markdownを保存します。")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if not args.dry_run and not config.privacy.allow_external_llm:
            raise RuntimeError("外部送信は無効です。まず --dry-run で確認し、送信する場合は privacy.allow_external_llm を true に設定してください。")
        days = config.analysis.days if args.days is None else args.days
        if not 1 <= days <= 90:
            raise RuntimeError("分析日数は1〜90日で指定してください。")
        generated = datetime.now(timezone.utc)
        today = generated.astimezone(ZoneInfo(config.analysis.timezone)).date()
        end = args.end_date or today
        if end > today:
            raise RuntimeError("終了日は今日以前を指定してください。")
        profile = profile_at(config.child, end - timedelta(days=1))
        settings = load_llm_settings(args.env_file, require_credentials=not args.dry_run)
        output = safe_output_dir(args.output_dir, args.data_dir)
        analysis = build_analysis(
            args.data_dir, days=days, end_date=end, timezone_name=config.analysis.timezone,
            now=generated, include_event_details=config.privacy.send_event_details,
            include_memos=config.privacy.send_memos,
        )
        payload = {
            "profile": profile, "analysis": analysis,
            "focus": list(config.analysis.focus), "context": config.analysis.context,
            "reference_guidance": REFERENCE_GUIDANCE,
        }
        body = build_request_body(payload, settings, config)
        request_json = json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if len(request_json.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise RuntimeError("送信予定データが上限を超えました。対象日数を減らすか、メモ・個別記録の送信を無効にしてください。")
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        prefix = generated.strftime("%Y-%m-%dT%H-%M-%SZ-") + ("preview-" if args.dry_run else "report-")
        run_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=output))
        private_write(run_dir / "metrics.json", json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        # Store the exact body sent; authorization headers are never persisted.
        private_write(run_dir / "request.json", request_json)
        advice = None
        if not args.dry_run:
            apply_proxy_settings(args.env_file)
            advice = generate_advice(body, settings, timeout_seconds=config.llm.timeout_seconds)
        report = render_report(
            analysis, advice, model=settings.model, generated_at=generated,
            profile=profile, dry_run=args.dry_run,
        )
        private_write(run_dir / "report.md", report)
        print(f"{'Preview (no API request)' if args.dry_run else 'Report'} saved: {run_dir / 'report.md'}")
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
    except OSError:
        # File and transport exception strings can contain sensitive paths or URLs.
        print("Error: 設定・入力・出力へのアクセスに失敗しました。完了レポートは生成されていません。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
