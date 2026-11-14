"""Isolated audio-separator worker; never downloads missing model files."""

from __future__ import annotations

import argparse
import json
import os
import socket
from importlib.metadata import version
from pathlib import Path

from .plan import sha256


def pinned_yaml_path(root: Path, name: str, allowed: dict[str, str]) -> Path:
    """Resolve YAML only within the explicit, hashed model file set."""
    path = Path(name)
    if path.is_absolute() or path.name != name or name not in allowed:
        raise ValueError("YAML configuration must be an allowed local model companion")
    candidate = root / name
    if sha256(candidate) != allowed[name]:
        raise ValueError("YAML companion hash mismatch")
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("model", "allowed", "audio", "output"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    allowed = json.loads(Path(args.allowed).read_text(encoding="utf-8"))
    model = Path(args.model).absolute()
    root = model.parent
    os.environ["AUDIO_SEPARATOR_MODEL_DIR"] = str(root)

    def denied(*_args, **_kwargs):
        raise OSError("Network access is disabled in the separator worker")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    from audio_separator.separator import Separator

    class OfflineSeparator(Separator):
        def download_file_if_not_exists(self, url, output_path, verify_checksum=False):
            path = Path(output_path).absolute()
            if (
                path.parent != root
                or path.name not in allowed
                or sha256(path) != allowed[path.name]
            ):
                raise FileNotFoundError(f"Missing or changed offline model file: {path.name}")

        def download_file_from_first_available(self, urls, output_path):
            self.download_file_if_not_exists("", output_path)

        def load_model_data_from_yaml(self, yaml_config_filename):
            path = pinned_yaml_path(root, yaml_config_filename, allowed)
            return super().load_model_data_from_yaml(str(path))

    for name, digest in allowed.items():
        if Path(name).name != name or sha256(root / name) != digest:
            raise ValueError("Offline model file hash mismatch")
    separator = OfflineSeparator(
        model_file_dir=str(root),
        output_dir=args.output,
        output_format="WAV",
        sample_rate=48000,
        normalization_threshold=1.0,
    )
    separator.load_model(model_filename=model.name)
    separator.separate(
        args.audio,
        {
            "Vocals": "vocal_candidate",
            "Instrumental": "background_candidate",
            "No Vocals": "background_candidate",
        },
    )
    output = Path(args.output)
    vocals = output / "vocal_candidate.wav"
    background = output / "background_candidate.wav"
    if not vocals.is_file() or not background.is_file():
        raise RuntimeError("The model did not produce identifiable vocal and background stems")
    for name, digest in allowed.items():
        if sha256(root / name) != digest:
            raise ValueError("Offline model file changed during separation")
    (output / "worker-result.json").write_text(
        json.dumps(
            {
                "version": version("audio-separator"),
                "vocals": vocals.name,
                "background": background.name,
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
