"""P7D-A: explicit offline P7B/P7C inputs, hash verification, Population A only."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kalshi_bot.research.continuous_state import load_and_analyze, write_report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_dir',type=Path)
    parser.add_argument('--p7c-report',required=True,type=Path)
    parser.add_argument('--p7c-sha256',required=True)
    parser.add_argument('--artifact-index',required=True,type=Path)
    parser.add_argument('--aggregate-sha256',required=True)
    parser.add_argument('--within-asset-scaling',action='store_true')
    parser.add_argument('--out-dir',required=True,type=Path)
    args=parser.parse_args()
    result=load_and_analyze(args.input_dir,args.p7c_report,args.artifact_index,
                            p7c_sha256=args.p7c_sha256,aggregate_sha256=args.aggregate_sha256,
                            scale=args.within_asset_scaling)
    print('P7D-A report: '+str(write_report(result,args.out_dir)))


if __name__=='__main__':
    main()
