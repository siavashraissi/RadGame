import pandas as pd
import re
from tqdm import tqdm
import argparse
import os
import json

# keyword patterns for interstitial pattern classes
keyword_map = {
    "Interstitial pattern": [r"interstitial pattern"],
    "Reticular pattern": [r"reticular", r"reticulation", r"reticulated"],
    "Reticulonodular pattern": [r"reticulonodular", r"reticulo[- ]?nodular"],
    "Ground glass pattern": [r"ground glass", r"ground-glass"],
    "Kerley lines": [r"kerley", r"kerley['']s", r"kerleys", r"kerley[- ]?b", r"kerley[- ]?a", r"kerley[- ]?b lines", r"septal lines"],
    "Miliary opacities": [r"miliary", r"miliary pattern", r"milliary", r"milliary pattern"],
    "Honeycombing": [r"honeycomb", r"honeycombing", r"honeycombed"],
}

def search_keywords(text, patterns):
    if pd.isna(text):
        return False
    text = text.lower()
    return any(re.search(pattern, text) for pattern in patterns)
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Label CSV rows based on findings/impressions keywords.")
    parser.add_argument('--input_csvs', nargs='+', help='One or more input CSV files to process')
    parser.add_argument('--output', '-o', default='output_with_classes.csv', help='Output CSV file name (default: output_with_classes.csv)')
    args = parser.parse_args()

    dfs = [pd.read_csv(csv) for csv in args.input_csvs]
    df = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]

    for class_name, patterns in tqdm(keyword_map.items(), desc="Processing classes"):
        tqdm.pandas(desc=f"Searching {class_name}")
        df[class_name] = df.progress_apply(
            lambda row: int(
                search_keywords(str(row.get("Findings", "")), patterns) or
                search_keywords(str(row.get("Impressions", "")), patterns)
            ),
            axis=1
        )

    class_cols = list(keyword_map.keys())
    df_filtered = df[df[class_cols].sum(axis=1) > 0].copy()

    stats = {class_name: int(df_filtered[class_name].sum()) for class_name in class_cols}

    df_filtered.to_csv(args.output, index=False)
    print(f"Saved output to {args.output}")

    json_path = os.path.splitext(args.output)[0] + ".json"
    with open(json_path, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"Saved stats to {json_path}")
