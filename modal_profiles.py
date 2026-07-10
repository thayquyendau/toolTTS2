import json
from pathlib import Path
from typing import Any


WORKER_DIR = Path(__file__).resolve().parent
PROFILE_CONFIG_PATH = WORKER_DIR / "config" / "modal_profiles.json"


class ModalProfileError(ValueError):
    pass


def _load_profile_document() -> dict[str, Any]:
    if not PROFILE_CONFIG_PATH.exists():
        raise ModalProfileError(f"Modal profile config not found: {PROFILE_CONFIG_PATH}")
    try:
        data = json.loads(PROFILE_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModalProfileError(f"Invalid JSON in {PROFILE_CONFIG_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModalProfileError("Modal profile config must be a JSON object.")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ModalProfileError("Modal profile config must define a non-empty profiles object.")
    return data


def _normalize_profile_key(value: str | None, default_profile: str) -> str:
    normalized = str(value or "").strip()
    return normalized or default_profile


def _sanitize_profile(profile_key: str, raw: dict[str, Any]) -> dict[str, Any]:
    token_id_env = str(raw.get("token_id_env") or "").strip()
    token_secret_env = str(raw.get("token_secret_env") or "").strip()
    if not token_id_env or not token_secret_env:
        raise ModalProfileError(
            f"Profile '{profile_key}' must define token_id_env and token_secret_env."
        )
    return {
        "key": profile_key,
        "label": str(raw.get("label") or profile_key).strip() or profile_key,
        "token_id_env": token_id_env,
        "token_secret_env": token_secret_env,
        "modal_app_name": str(raw.get("modal_app_name") or "tooltucode-gpu-v2").strip() or "tooltucode-gpu-v2",
        "modal_tts_gpu": str(raw.get("modal_tts_gpu") or "L4").strip() or "L4",
        "modal_xtts_artifact_volume": str(raw.get("modal_xtts_artifact_volume") or "tooltucode-xtts-artifacts").strip() or "tooltucode-xtts-artifacts",
        "modal_xtts_artifact_prefix": str(raw.get("modal_xtts_artifact_prefix") or "xtts-jobs").strip() or "xtts-jobs",
    }


def get_modal_profiles_config() -> dict[str, Any]:
    data = _load_profile_document()
    default_profile = _normalize_profile_key(data.get("default_profile"), "default")
    profiles = {
        key: _sanitize_profile(key, value)
        for key, value in data["profiles"].items()
        if isinstance(value, dict)
    }
    if not profiles:
        raise ModalProfileError("Modal profile config does not contain any valid profile entries.")
    if default_profile not in profiles:
        raise ModalProfileError(f"Default profile '{default_profile}' is not defined in profiles.")
    return {
        "default_profile": default_profile,
        "profiles": profiles,
    }


def get_modal_profile(profile_key: str | None = None) -> dict[str, Any]:
    config = get_modal_profiles_config()
    selected_key = _normalize_profile_key(profile_key, config["default_profile"])
    profile = config["profiles"].get(selected_key)
    if profile is None:
        raise ModalProfileError(f"Unknown Modal profile: {selected_key}")
    return dict(profile)


def list_modal_profiles() -> dict[str, Any]:
    config = get_modal_profiles_config()
    return {
        "default_profile": config["default_profile"],
        "profiles": [
            {
                "key": profile["key"],
                "label": profile["label"],
                "modal_app_name": profile["modal_app_name"],
                "modal_tts_gpu": profile["modal_tts_gpu"],
                "modal_xtts_artifact_volume": profile["modal_xtts_artifact_volume"],
                "modal_xtts_artifact_prefix": profile["modal_xtts_artifact_prefix"],
            }
            for profile in config["profiles"].values()
        ],
    }
