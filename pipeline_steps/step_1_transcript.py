import html
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import requests

from job_status import append_job_log, update_step_status, update_transcript_data
from pipeline_steps.common import ProcessingError


def _extract_video_id(url: str) -> str | None:
    url_str = str(url)
    parsed_url = urlparse(url_str)
    if "youtube.com" in parsed_url.netloc:
        return parse_qs(parsed_url.query).get("v", [None])[0]
    if "youtu.be" in parsed_url.netloc:
        return parsed_url.path[1:]
    return None


def _build_youtube_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _extract_innertube_api_key(page_html: str) -> str:
    patterns = [
        r'"INNERTUBE_API_KEY":"([^"]+)"',
        r'"innertubeApiKey":"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html)
        if match:
            return match.group(1)
    raise RuntimeError("Could not find INNERTUBE_API_KEY in YouTube watch page.")


def _extract_caption_tracks(player_response: dict) -> list[dict]:
    captions = player_response.get("captions") or {}
    renderer = captions.get("playerCaptionsTracklistRenderer") or {}
    tracks = renderer.get("captionTracks") or []
    if not tracks:
        raise RuntimeError("No captionTracks found in InnerTube player response.")
    return tracks


def _select_caption_track(caption_tracks: list[dict]) -> dict:
    preferred_codes = ("vi", "en")
    for preferred_code in preferred_codes:
        for track in caption_tracks:
            language_code = str(track.get("languageCode", "")).lower()
            if language_code == preferred_code and track.get("kind") != "asr":
                return track
        for track in caption_tracks:
            language_code = str(track.get("languageCode", "")).lower()
            if language_code.startswith(preferred_code):
                return track
    return caption_tracks[0]


def _extract_text_from_transcript_xml(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse transcript XML: {exc}") from exc

    fragments = []
    for text_node in root.findall(".//text"):
        fragment = html.unescape("".join(text_node.itertext())).strip()
        if fragment:
            fragments.append(re.sub(r"\s+", " ", fragment))

    if not fragments:
        for paragraph_node in root.findall(".//p"):
            fragment_parts = []
            direct_text = html.unescape((paragraph_node.text or "")).strip()
            if direct_text:
                fragment_parts.append(direct_text)
            for segment_node in paragraph_node.findall(".//s"):
                segment_text = html.unescape("".join(segment_node.itertext())).strip()
                if segment_text:
                    fragment_parts.append(segment_text)
            merged = re.sub(r"\s+", " ", " ".join(fragment_parts)).strip()
            if merged:
                fragments.append(merged)

    return " ".join(fragments).strip()


def _extract_text_from_transcript_json3(payload: dict) -> str:
    fragments = []
    for event in payload.get("events", []):
        segment_parts = []
        for segment in event.get("segs", []) or []:
            text = html.unescape(str(segment.get("utf8", ""))).strip()
            if text:
                segment_parts.append(text)
        merged = re.sub(r"\s+", " ", " ".join(segment_parts)).strip()
        if merged:
            fragments.append(merged)
    return " ".join(fragments).strip()


def _write_transcript_debug_artifact(job_dir: str, filename: str, content: str) -> str:
    path = os.path.join(job_dir, filename)
    with open(path, "w", encoding="utf-8", errors="replace") as file_obj:
        file_obj.write(content)
    return path


def _extract_transcript_text(response: requests.Response, job_dir: str) -> str:
    content_type = (response.headers.get("Content-Type") or "").lower()
    body = response.text or ""
    preview = re.sub(r"\s+", " ", body[:500]).strip()
    append_job_log(job_dir, f"Step 1 transcript response content-type: {content_type or 'unknown'}")
    if preview:
        append_job_log(job_dir, f"Step 1 transcript response preview: {preview}")
    if not body.strip():
        return ""
    if "json" in content_type or body.lstrip().startswith("{"):
        debug_path = _write_transcript_debug_artifact(job_dir, "transcript_response.json", body)
        append_job_log(job_dir, f"Step 1 saved raw transcript response: {debug_path}")
        try:
            return _extract_text_from_transcript_json3(json.loads(body))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse transcript JSON3: {exc}") from exc
    debug_path = _write_transcript_debug_artifact(job_dir, "transcript_response.xml", body)
    append_job_log(job_dir, f"Step 1 saved raw transcript response: {debug_path}")
    return _extract_text_from_transcript_xml(body)


def _fetch_inner_tube_player_response(video_id: str, youtube_url: str, job_dir: str) -> dict:
    session = _build_youtube_session()
    watch_response = session.get(youtube_url, timeout=20)
    watch_response.raise_for_status()
    api_key = _extract_innertube_api_key(watch_response.text)
    append_job_log(job_dir, "Step 1 extracted INNERTUBE_API_KEY from watch page.")
    player_url = f"https://www.youtube.com/youtubei/v1/player?key={api_key}"
    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "20.10.38",
            }
        },
        "videoId": video_id,
    }
    append_job_log(job_dir, "Step 1 calling InnerTube player endpoint for captionTracks.")
    player_response = session.post(player_url, json=payload, timeout=20)
    player_response.raise_for_status()
    return player_response.json()


def _download_transcript_from_caption_track(track: dict, job_dir: str) -> str:
    base_url = track.get("baseUrl")
    if not base_url:
        raise RuntimeError("Selected caption track does not include baseUrl.")
    response = requests.get(base_url, timeout=20)
    response.raise_for_status()
    return _extract_transcript_text(response, job_dir)


def _download_transcript_with_innertube(video_id: str, youtube_url: str, job_dir: str) -> str:
    player_response = _fetch_inner_tube_player_response(video_id, youtube_url, job_dir)
    caption_tracks = _extract_caption_tracks(player_response)
    selected_track = _select_caption_track(caption_tracks)
    append_job_log(
        job_dir,
        "Step 1 selected caption track: "
        f"language={selected_track.get('languageCode')}, kind={selected_track.get('kind', 'manual')}",
    )
    return _download_transcript_from_caption_track(selected_track, job_dir)


def step_1_get_transcript(youtube_url: str, job_dir: str) -> str:
    update_step_status(job_dir, "step_1_get_transcript", "running", "Fetching transcript from YouTube.")
    append_job_log(job_dir, f"Step 1 start: {youtube_url}")
    video_id = _extract_video_id(youtube_url)
    if not video_id:
        message = "Invalid YouTube URL or could not extract video ID."
        update_step_status(job_dir, "step_1_get_transcript", "failure", message)
        raise ProcessingError(message)
    try:
        full_text = _download_transcript_with_innertube(video_id, youtube_url, job_dir)
    except Exception as exc:
        message = f"Could not fetch transcript with InnerTube: {exc}"
        update_step_status(job_dir, "step_1_get_transcript", "failure", message, detail=str(exc))
        raise ProcessingError(message) from exc
    if not full_text.strip():
        message = "Transcript response was empty."
        update_step_status(job_dir, "step_1_get_transcript", "failure", message)
        raise ProcessingError(message)
    out_path = os.path.join(job_dir, "transcript.txt")
    with open(out_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(full_text)
    update_transcript_data(job_dir, full_text)
    update_step_status(job_dir, "step_1_get_transcript", "success", "Transcript saved.")
    return out_path
