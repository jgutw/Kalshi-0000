"""Build P7B artifacts from explicitly declared offline JSONL exports."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kalshi_bot.research.market_state_dataset import build, safe_path, write_artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest_path = safe_path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    for source in manifest["sources"]:
        source["path"] = str(manifest_path.parent / source["path"])
    result = build(manifest)
    write_artifacts(result, args.out_dir)
    print("Created six P7B artifacts in " + str(args.out_dir.resolve()))


if __name__ == "__main__":
    main()
