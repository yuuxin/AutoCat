"""Integer-clock helpers shared by the editor, renderer, and sync checks."""

from __future__ import annotations

SAMPLE_RATE = 48_000
SYNC_TOLERANCE_SECONDS = 0.020


def seconds_to_frames(seconds: float, fps: int) -> int:
    return max(1, int(round(float(seconds) * int(fps))))


def frames_to_seconds(frames: int, fps: int) -> float:
    return int(frames) / int(fps)


def frames_to_samples(frames: int, fps: int, sample_rate: int = SAMPLE_RATE) -> int:
    return int(round(int(frames) * int(sample_rate) / int(fps)))


def seconds_to_samples(seconds: float, sample_rate: int = SAMPLE_RATE) -> int:
    return max(1, int(round(float(seconds) * int(sample_rate))))


def samples_to_seconds(samples: int, sample_rate: int = SAMPLE_RATE) -> float:
    return int(samples) / int(sample_rate)


def build_target_clock(narration_duration: float, fps: int,
                       tail_duration: float = 0.5) -> dict:
    """Quantize narration + tail to one video frame and matching audio samples."""
    target_frames = seconds_to_frames(float(narration_duration) + float(tail_duration), fps)
    target_samples = frames_to_samples(target_frames, fps)
    return {
        "sample_rate": SAMPLE_RATE,
        "narration_duration": float(narration_duration),
        "tail_duration": float(tail_duration),
        "target_video_frames": target_frames,
        "target_audio_samples": target_samples,
        "final_duration": frames_to_seconds(target_frames, fps),
    }


def quantize_frame(seconds: float, fps: int, *, minimum: int = 0,
                   maximum: int | None = None) -> int:
    frame = max(int(minimum), int(round(float(seconds) * int(fps))))
    if maximum is not None:
        frame = min(frame, int(maximum))
    return frame


def apply_integer_timeline(script: dict, narration_duration: float,
                           fps: int, tail_duration: float = 0.5) -> dict:
    """Attach authoritative integer clocks and quantize clip boundaries in-place."""
    clock = build_target_clock(narration_duration, fps, tail_duration)
    script.update(clock)
    script["total_duration"] = clock["final_duration"]

    target_frames = clock["target_video_frames"]
    transition_frames = max(0, int(round(
        float(script.get("transition_duration") or 0.3) * fps
    )))
    previous_end = 0
    clips = script.get("clips", [])
    for index, clip in enumerate(clips):
        start = quantize_frame(
            clip.get("start_time", previous_end / fps), fps,
            minimum=previous_end, maximum=target_frames,
        )
        end = quantize_frame(
            clip.get("end_time", start / fps), fps,
            minimum=start + 1, maximum=target_frames,
        )
        if index == len(clips) - 1:
            end = target_frames
        clip_transition_frames = transition_frames if index % 5 != 0 else 0
        clip["start_frame"] = start
        clip["end_frame"] = end
        clip["duration_frames"] = end - start + clip_transition_frames
        clip["transition_frames"] = clip_transition_frames
        clip["transition_start_frame"] = max(0, start - clip_transition_frames)
        clip["transition_end_frame"] = start
        clip["start_time"] = frames_to_seconds(start, fps)
        clip["end_time"] = frames_to_seconds(end, fps)
        clip["duration"] = frames_to_seconds(clip["duration_frames"], fps)
        clip["transition_duration"] = frames_to_seconds(transition_frames, fps)
        previous_end = end
    return script
