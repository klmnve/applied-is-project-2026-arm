"""Explicit one-time public model file download; never used by the server."""

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare public multilingual embedding files locally"
    )
    parser.add_argument("--model", default="intfloat/multilingual-e5-small")
    parser.add_argument("--output", type=Path, default=Path("models/multilingual-e5-small"))
    args = parser.parse_args()
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model, device="cpu", trust_remote_code=False, token=False)
    model.save(str(args.output))
    print(f"Local model saved: {args.output.resolve()}")


if __name__ == "__main__":
    main()
