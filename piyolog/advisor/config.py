"""Strict, small configuration surface for the local advisor."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


@dataclass(frozen=True)
class ChildConfig:
    birth_date: date
    corrected_birth_date: date | None = None


@dataclass(frozen=True)
class AnalysisConfig:
    days: int = 14
    timezone: str = "Asia/Tokyo"
    focus: tuple[str, ...] = ()
    context: str = ""


@dataclass(frozen=True)
class PrivacyConfig:
    allow_external_llm: bool = False
    send_event_details: bool = False
    send_memos: bool = False


@dataclass(frozen=True)
class LLMConfig:
    max_output_tokens: int = 4096
    timeout_seconds: int = 120
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class AdvisorConfig:
    child: ChildConfig
    analysis: AnalysisConfig = AnalysisConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    llm: LLMConfig = LLMConfig()


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


def _table(value: object, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown key in {name}")
    return value


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _int(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _str(value: object, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{name} must be a string" + (" with content" if nonempty else ""))
    return value


def _date(value: object, name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{name} must be a date (YYYY-MM-DD)")


def load_config(path: Path) -> AdvisorConfig:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("cannot read advisor configuration") from exc
    root = _table(raw, "root", {"child", "analysis", "privacy", "llm"})
    child = _table(root.get("child"), "child", {"birth_date", "corrected_birth_date"})
    if "birth_date" not in child:
        raise ValueError("child.birth_date is required")
    birth_date = _date(child["birth_date"], "child.birth_date")
    corrected_birth_date = (
        _date(child["corrected_birth_date"], "child.corrected_birth_date")
        if "corrected_birth_date" in child else None
    )
    if corrected_birth_date is not None and corrected_birth_date < birth_date:
        raise ValueError("child.corrected_birth_date must be on or after child.birth_date")

    analysis = _table(root.get("analysis", {}), "analysis", {"days", "timezone", "focus", "context"})
    days = _int(analysis.get("days", 14), "analysis.days", 1, 90)
    timezone = _str(analysis.get("timezone", "Asia/Tokyo"), "analysis.timezone", nonempty=True)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("analysis.timezone is invalid") from exc
    focus = analysis.get("focus", [])
    if not isinstance(focus, list) or any(not isinstance(item, str) or not item.strip() for item in focus):
        raise ValueError("analysis.focus must be a list of nonempty strings")
    context = _str(analysis.get("context", ""), "analysis.context")

    privacy = _table(root.get("privacy", {}), "privacy", {"allow_external_llm", "send_event_details", "send_memos"})
    llm = _table(root.get("llm", {}), "llm", {"max_output_tokens", "timeout_seconds", "reasoning_effort"})
    effort = llm.get("reasoning_effort")
    if effort is not None:
        effort = _str(effort, "llm.reasoning_effort", nonempty=True)
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("llm.reasoning_effort is invalid")
    return AdvisorConfig(
        child=ChildConfig(birth_date, corrected_birth_date),
        analysis=AnalysisConfig(days, timezone, tuple(focus), context),
        privacy=PrivacyConfig(*(
            _bool(privacy.get(key, False), f"privacy.{key}")
            for key in ("allow_external_llm", "send_event_details", "send_memos")
        )),
        llm=LLMConfig(
            _int(llm.get("max_output_tokens", 4096), "llm.max_output_tokens", 16, 100000),
            _int(llm.get("timeout_seconds", 120), "llm.timeout_seconds", 1, 3600),
            effort,
        ),
    )


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot read environment file") from exc
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            values.setdefault(name, value)
    return values


def _validate_base_url(url: str) -> None:
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("ADVISOR_LLM_BASE_URL is invalid")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ADVISOR_LLM_BASE_URL is invalid") from exc
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or url.endswith("?") or url.endswith("#")
            or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
            ))):
        raise ValueError("ADVISOR_LLM_BASE_URL is invalid")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ADVISOR_LLM_BASE_URL is invalid")


def load_llm_settings(env_file: Path, *, require_credentials: bool = True) -> LLMSettings:
    values = _read_env_file(env_file)

    def get(name: str) -> str:
        return os.environ.get(name, values.get(name, ""))

    provider = get("ADVISOR_LLM_PROVIDER") or "responses"
    if provider != "responses":
        raise ValueError("ADVISOR_LLM_PROVIDER must be responses")
    base_url = get("ADVISOR_LLM_BASE_URL")
    model = get("ADVISOR_LLM_MODEL")
    key_name = get("ADVISOR_LLM_API_KEY_ENV")
    if key_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name):
        raise ValueError("ADVISOR_LLM_API_KEY_ENV is invalid")
    api_key = get(key_name) if key_name else get("ADVISOR_LLM_API_KEY")
    if require_credentials:
        if not base_url:
            raise ValueError("ADVISOR_LLM_BASE_URL is required")
        if not model.strip():
            raise ValueError("ADVISOR_LLM_MODEL is required")
        if not api_key:
            raise ValueError("ADVISOR_LLM_API_KEY is required")
    if base_url:
        _validate_base_url(base_url)
    if model and (not model.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in model)):
        raise ValueError("ADVISOR_LLM_MODEL is invalid")
    if api_key and any(not 33 <= ord(ch) <= 126 for ch in api_key):
        raise ValueError("ADVISOR_LLM_API_KEY is invalid")
    return LLMSettings(provider, base_url, model, api_key)
