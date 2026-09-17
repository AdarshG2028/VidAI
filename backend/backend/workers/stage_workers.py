"""Stage worker plugins for the example media pipeline (Phase 4's 3-stage
milestone plus Phase 5's 6-stage stretch chain). No real AI, no storage
layer — each worker derives a realistic-shaped fake output from the prior
stage's output (injected by StageProcessingService, see base.py), so
swapping in a real model later means changing what's inside process(),
not the pipeline shape itself.
"""

from typing import Any

from backend.workers.base import StageMessage, Worker


class FrameExtractionWorker(Worker):
    name = "frame_extraction"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        frame_count = 5
        fps = 24
        return {
            "frame_count": frame_count,
            "fps": fps,
            "frames": [
                {"frame_index": i, "timestamp_ms": round(i * 1000 / fps)}
                for i in range(frame_count)
            ],
        }


class VisionDetectionWorker(Worker):
    """Phase 4's 3-stage milestone worker — kept as-is for that workflow;
    Phase 5's chain uses GroundingDinoWorker + Sam2Worker instead."""

    name = "vision_detection"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        return {"processed_by": self.name, "job_id": str(message.job_id), "detections": []}


class GroundingDinoWorker(Worker):
    name = "grounding_dino"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        frames = (previous_output or {}).get("frames", [])
        return {
            "frame_count": len(frames),
            "detections": [
                {
                    "frame_index": frame["frame_index"],
                    "boxes": [{"label": "person", "score": 0.91, "bbox": [10, 20, 110, 240]}],
                }
                for frame in frames
            ],
        }


class Sam2Worker(Worker):
    name = "sam2"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        detections = (previous_output or {}).get("detections", [])
        return {
            "frame_count": len(detections),
            "masks": [
                {
                    "frame_index": frame["frame_index"],
                    "objects": [
                        {
                            "label": box["label"],
                            "mask_id": f"mask_{frame['frame_index']}_{i}",
                            "area_px": 8320,
                            "bbox": box["bbox"],
                        }
                        for i, box in enumerate(frame["boxes"])
                    ],
                }
                for frame in detections
            ],
        }


class TrackingWorker(Worker):
    name = "tracking"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        masks = (previous_output or {}).get("masks", [])
        tracks_by_label: dict[str, list[int]] = {}
        for frame in masks:
            for obj in frame["objects"]:
                tracks_by_label.setdefault(obj["label"], []).append(frame["frame_index"])

        tracks = [
            {"track_id": f"track_{i}", "label": label, "frame_indices": frame_indices}
            for i, (label, frame_indices) in enumerate(tracks_by_label.items())
        ]
        return {"track_count": len(tracks), "tracks": tracks}


class ProPainterWorker(Worker):
    name = "propainter"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        tracks = (previous_output or {}).get("tracks", [])
        removed_track_ids = [t["track_id"] for t in tracks]
        inpainted_frame_count = len({i for t in tracks for i in t["frame_indices"]})
        return {
            "removed_track_ids": removed_track_ids,
            "inpainted_frame_count": inpainted_frame_count,
            "output_reference": f"propainter_output_{message.job_id}.mp4",
        }


class RenderingWorker(Worker):
    name = "rendering"

    async def process(
        self, message: StageMessage, previous_output: dict[str, Any] | None
    ) -> dict[str, Any]:
        output_reference = (previous_output or {}).get("output_reference")
        return {
            "output_path": f"renders/{message.job_id}.mp4",
            "based_on_output_reference": output_reference,
            "resolution": "1920x1080",
        }
