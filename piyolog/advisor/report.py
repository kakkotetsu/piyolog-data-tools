"""Render computed observations separately from model prose."""

from __future__ import annotations

from datetime import datetime


COUNT_LABELS = {
    "Pee": "おしっこ", "Poop": "うんち", "BreastFeeding": "授乳",
    "Formula": "ミルク（Formula）", "Milk": "ミルク（Milk）", "Solid": "離乳食",
}


def _safe(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _number(value: object, *, suffix: str = "") -> str:
    if value is None or type(value) not in (int, float):
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".") + suffix


def render_report(
    analysis: dict,
    advice: str | None,
    *,
    model: str,
    generated_at: datetime,
    profile: dict,
    dry_run: bool = False,
) -> str:
    period = analysis.get("period", {})
    coverage = analysis.get("coverage", {})
    summary = analysis.get("summary", {})
    daily = analysis.get("daily", [])
    lines = [
        "# 育児記録の振り返り",
        "",
        f"生成日時: {_safe(generated_at.isoformat(timespec='seconds'))}",
        f"対象期間: {_safe(period.get('start', '—'))} 〜 {_safe(period.get('end_exclusive', '—'))}（終了日を含まない）",
        f"タイムゾーン: {_safe(period.get('timezone', '—'))}",
        f"対象月齢: {_safe(profile.get('age_months', '—'))}か月（{_safe(profile.get('age_as_of', '—'))} 時点）",
    ]
    if profile.get("corrected_age_months") is not None:
        lines.append(f"修正月齢: {_safe(profile['corrected_age_months'])}か月")
    lines += [
        f"AIモデル: {_safe(model or '未設定')}",
        "",
        "## 集計（プログラムによる計算）",
        "",
        "取得範囲が揃った日だけを日平均の分母に含めます。取得範囲が部分的な日や、睡眠・覚醒が不明な時間をゼロ件・睡眠ゼロ時間とは扱いません。入力記録そのものの完全性は保証できません。",
        "",
        f"要求日数: {_number(period.get('requested_days'))}日 / 取得範囲が揃った日: {_number(coverage.get('complete_days'))}日",
        f"最新スナップショット: {_safe(coverage.get('latest_snapshot_at', '—'))}",
        "",
        "### 日平均",
        "",
        "| 項目 | 平均 | 集計日数 |",
        "| --- | ---: | ---: |",
    ]
    counts_avg = summary.get("counts_average") or {}
    counts_days = summary.get("counts_days")
    for key, label in COUNT_LABELS.items():
        days = counts_days.get(key) if isinstance(counts_days, dict) else counts_days
        lines.append(f"| {label} | {_number(counts_avg.get(key))}回/日 | {_number(days)}日 |")
    lines.append(f"| 睡眠 | {_number(summary.get('sleep_average_hours'))}時間/日 | {_number(summary.get('sleep_days'))}日 |")
    lines += [
        "",
        "### 睡眠と夜間の記録",
        "",
        "| 項目 | 集計値 |",
        "| --- | ---: |",
        f"| 1日の睡眠時間の範囲 | {_number(summary.get('sleep_min_hours'))}〜{_number(summary.get('sleep_max_hours'))}時間 |",
    ]
    nights = summary.get("nights") or {}
    lines += [
        f"| 夜間の集計対象 | {_number(nights.get('nights'))}夜 |",
        f"| 夜間の起床回数の平均 | {_number(nights.get('wakings_average'))}回/夜 |",
        f"| 夜間で最も長い睡眠の平均 | {_number(nights.get('longest_sleep_average_hours'))}時間/夜 |",
        "",
        "### 食事・授乳と体重の記録",
        "",
        "| 項目 | 集計値 |",
        "| --- | ---: |",
    ]
    feeding = summary.get("breastfeeding_minutes") or {}
    lines += [
        f"| 授乳時間の記録件数 | {_number(feeding.get('records'))}件 |",
        f"| 記録された授乳時間の平均 | {_number(feeding.get('average_minutes'))}分/件 |",
        f"| 体重の記録件数 | {_number(summary.get('weight_record_count'))}件 |",
        "",
        "ミルク量は記録された単位ごとに集計します。これらの記録だけでは栄養量の充足や授乳量の適否は判断できません。",
        "",
        "| ミルク量の単位 | 記録件数 | 記録量合計 | 1件平均 |",
        "| --- | ---: | ---: | ---: |",
    ]
    volumes = summary.get("formula_milk_volume_by_unit") or {}
    for unit, values in sorted(volumes.items()):
        lines.append(
            f"| {_safe(unit)} | {_number(values.get('records'))}件 "
            f"| {_number(values.get('total'))}{_safe(unit)} "
            f"| {_number(values.get('average'))}{_safe(unit)} |"
        )
    if not volumes:
        lines.append("| 記録なし | — | — | — |")
    hours = summary.get("solid_hour_counts")
    if isinstance(hours, list) and len(hours) == 24:
        lines += ["", "離乳食の記録時刻（時刻別件数）:", "", "| 時間帯 | 記録件数 |", "| --- | ---: |"]
        for hour, count in enumerate(hours):
            if count:
                lines.append(f"| {hour:02d}:00〜{hour:02d}:59 | {_number(count)}件 |")
        if not any(hours):
            lines.append("| 記録なし | 0件 |")
    lines += [
        "",
        "### 日別の記録状況",
        "",
        "| 日付 | 取得範囲 | 取得範囲が揃った日 | 判明している睡眠 | 睡眠・覚醒が不明な時間 |",
        "| --- | ---: | :---: | ---: | ---: |",
    ]
    for day in daily:
        lines.append(
            f"| {_safe(day.get('date', '—'))} | {_number(day.get('coverage_hours'), suffix='時間')} "
            f"| {'はい' if day.get('complete') is True else 'いいえ'} "
            f"| {_number(day.get('known_sleep_hours'), suffix='時間')} "
            f"| {_number(day.get('unknown_sleep_hours'), suffix='時間')} |"
        )
    halves = summary.get("halves")
    if halves:
        earlier = halves.get("earlier") or {}
        later = halves.get("later") or {}
        differences = halves.get("later_minus_earlier") or {}
        lines += [
            "", "### 期間前半・後半", "",
            f"前半の集計対象: {_number(earlier.get('counts_days'))}日、後半の集計対象: {_number(later.get('counts_days'))}日。取得範囲が揃った日の平均を比較します。",
            "", "| 項目 | 前半の平均 | 後半の平均 | 後半 − 前半 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for key, label in COUNT_LABELS.items():
            lines.append(
                f"| {label} | {_number((earlier.get('counts_average') or {}).get(key))}回/日 "
                f"| {_number((later.get('counts_average') or {}).get(key))}回/日 "
                f"| {_number(differences.get(key))}回/日 |"
            )
    limitations = analysis.get("limitations") or []
    if limitations:
        lines += ["", "### 集計上の制約", ""]
        lines += [f"- {_safe(item)}" for item in limitations]
    lines += ["", "## AIによる文章", ""]
    if dry_run:
        lines.append("ドライランのためAIは実行していません。")
    elif advice:
        lines += ["以下はAIが作成した振り返りで、医療者が検証した医療指示ではありません。", "", advice.strip()]
    else:
        lines.append("AIによる文章はありません。")
    return "\n".join(lines) + "\n"
