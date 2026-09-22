"""P7C-A diagnostics on explicit offline P7B artifacts; no original-store reads."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kalshi_bot.research.variable_quality import load_and_analyze, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_dir', type=Path)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--quote-stale-seconds', type=float, default=None)
    parser.add_argument('--venue-stale-seconds', type=float, default=None)
    args = parser.parse_args()
    result = load_and_analyze(args.input_dir, quote_stale_seconds=args.quote_stale_seconds, venue_stale_seconds=args.venue_stale_seconds)
    path = write_report(result, args.out_dir)
    print('P7C-A diagnostics written: ' + str(path))


if __name__ == '__main__':
    main()
