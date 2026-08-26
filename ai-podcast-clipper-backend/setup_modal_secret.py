"""Create or update the Modal secret used by Dark Phoenix.

Usage:
    python setup_modal_secret.py
    python setup_modal_secret.py --env-file ../ai-podcast-clipper-frontend/.env.local
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

from dotenv import load_dotenv


SECRET_NAME = "ai-podcast-clipper-secret"
ENV_MAPPING = {
    "GEMINI_API_KEY": "GEMINI_API_KEY",
    "AUTH_TOKEN": "PROCESS_VIDEO_ENDPOINT_AUTH",
    "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION": "AWS_REGION",
    "S3_BUCKET_NAME": "S3_BUCKET_NAME",
}


def main() -> None:
    default_env_file = (
        pathlib.Path(__file__).resolve().parent.parent
        / "ai-podcast-clipper-frontend"
        / ".env.local"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=pathlib.Path, default=default_env_file)
    args = parser.parse_args()

    load_dotenv(args.env_file)
    values = {
        secret_key: os.getenv(source_key)
        for secret_key, source_key in ENV_MAPPING.items()
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Cannot create Modal secret; missing values: " + ", ".join(missing)
        )

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8", delete=False
        ) as temp_file:
            json.dump(values, temp_file)
            temp_path = pathlib.Path(temp_file.name)

        subprocess.run(
            [
                sys.executable,
                "-m",
                "modal",
                "secret",
                "create",
                "--from-json",
                str(temp_path),
                "--force",
                SECRET_NAME,
            ],
            check=True,
        )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print(f"Modal secret '{SECRET_NAME}' was created or updated.")


if __name__ == "__main__":
    main()
