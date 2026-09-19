import json
import math


def parse_clip_moments(raw_response: str, transcript_segments: list, min_duration: float = 15.0, max_duration: float = 65.0) -> list:
    cleaned_json_string = raw_response.strip()
    if cleaned_json_string.startswith("```"):
        first_newline = cleaned_json_string.find("\n")
        if first_newline != -1:
            cleaned_json_string = cleaned_json_string[first_newline + 1:]
    if cleaned_json_string.endswith("```"):
        cleaned_json_string = cleaned_json_string[:-3].strip()

    parsed_moments = json.loads(cleaned_json_string)
    if not isinstance(parsed_moments, list):
        raise ValueError("Gemini response must be a JSON list")

    transcript_end = max(
        (
            float(segment["end"])
            for segment in transcript_segments
            if segment.get("end") is not None
        ),
        default=0.0,
    )
    candidate_moments = []

    for moment in parsed_moments:
        if not isinstance(moment, dict):
            continue

        try:
            start_time = float(moment["start"])
            end_time = float(moment["end"])
        except (KeyError, TypeError, ValueError):
            continue

        duration = end_time - start_time
        if (
            not math.isfinite(start_time)
            or not math.isfinite(end_time)
            or start_time < 0
            or duration < min_duration
            or duration > max_duration
            or end_time > transcript_end + 1
        ):
            continue

        candidate_moments.append({"start": start_time, "end": end_time})

    valid_moments = []
    for moment in sorted(candidate_moments, key=lambda item: item["start"]):
        if valid_moments and moment["start"] < valid_moments[-1]["end"]:
            continue

        valid_moments.append(moment)
        if len(valid_moments) == 5:
            break

    return valid_moments
