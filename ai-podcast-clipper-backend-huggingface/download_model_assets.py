"""Download the TalkNet model assets during the Modal image build.

The vendored TalkNet code otherwise downloads these files at request time with
an obsolete gdown CLI flag. Building them into the image makes cold starts
deterministic and turns a missing model into a deployment failure instead of a
late, opaque processing failure.
"""

import os
import pathlib

import gdown


_BASE_ASD = pathlib.Path("/asd") if pathlib.Path("/asd").exists() else pathlib.Path(__file__).parent.resolve() / "asd"

MODEL_ASSETS = (
    (
        "1AbN9fCf9IexMxEKXLQY2KYBlb-IhSEea",
        _BASE_ASD / "pretrain_TalkSet.model",
        50_000_000,
    ),
    (
        "1KafnHz7ccT-3IyddBsL5yi2xGtxAKypt",
        _BASE_ASD / "model" / "faceDetector" / "s3fd" / "sfd_face.pth",
        75_000_000,
    ),
)


def ensure_model_asset(file_id: str, target: pathlib.Path, min_bytes: int) -> None:
    if target.exists() and target.stat().st_size >= min_bytes:
        print(f"Using existing model asset: {target}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_target = target.with_suffix(f"{target.suffix}.download")
    temporary_target.unlink(missing_ok=True)

    print(f"Downloading model asset to {target}")
    downloaded_path = gdown.download(
        id=file_id,
        output=str(temporary_target),
        quiet=False,
    )

    if not downloaded_path or not temporary_target.exists():
        raise RuntimeError(f"Model asset download did not produce {target}")

    downloaded_size = temporary_target.stat().st_size
    if downloaded_size < min_bytes:
        temporary_target.unlink(missing_ok=True)
        raise RuntimeError(
            f"Model asset {target} is unexpectedly small: {downloaded_size} bytes"
        )

    os.replace(temporary_target, target)


if __name__ == "__main__":
    for asset in MODEL_ASSETS:
        ensure_model_asset(*asset)
