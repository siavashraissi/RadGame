#!/usr/bin/env python3

import argparse
import ast
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

# openai check
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("Warning: openai package not installed")

try:
    from secretcodes import OPENAI_API_KEY as SECRET_FILE_KEY
except ImportError:
    SECRET_FILE_KEY = None

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

# update these paths for your system
REX_METADATA = "<path-to-rexgradient>/metadata/train_metadata.csv"
TEST_METADATA_JSON = "<path-to-rexgradient>/metadata/test_metadata.json"

SEED = 42
# target number of cases by finding count
TARGET_DISTRIBUTION = {0: 10, 1: 12, 2: 11, 3: 11, 4: 4, 5: 2}
TOTAL_SAMPLES = sum(TARGET_DISTRIBUTION.values())

# regex patterns
AGE_RE = re.compile(r"^(\d{1,4})([YMWD])$", re.IGNORECASE)
PRIOR_PATTERN = re.compile(
    r"\b(prior|previous|compared to|comparison|since (the )?prior|again noted|unchanged|"
    r"interval (change|improvement|worsening)|follow-?up)\b",
    re.IGNORECASE
)


def get_openai_client():
    """Initialize OpenAI client."""
    if not OPENAI_AVAILABLE:
        raise SystemExit("OpenAI package not installed. Run: pip install openai")
    api_key = os.environ.get("OPENAI_API_KEY") or SECRET_FILE_KEY
    if not api_key:
        raise SystemExit("OPENAI_API_KEY not set in environment or secretcodes.py")
    return OpenAI(api_key=api_key)


def is_adult_years(age):
    """Return True if age string indicates >=18 years."""
    if not age:
        return False
    age = age.strip().upper()
    m = AGE_RE.match(age)
    if not m:
        return False
    val, unit = int(m.group(1)), m.group(2)
    return unit == 'Y' and val >= 18


def is_under_18(age_str):
    """Check if age indicates under 18 years."""
    if not age_str:
        return False
    age_str = age_str.strip().upper()
    m = AGE_RE.match(age_str)
    if not m:
        return False
    val, unit = int(m.group(1)), m.group(2)
    if unit == 'Y':
        return val < 18
    return unit in {'M', 'W', 'D'}


def extract_positive_findings(raw_output):
    """Parse OpenAI response to extract list of findings."""
    cleaned = raw_output.strip()
    
    # Remove angle brackets
    if cleaned.startswith('<') and cleaned.endswith('>'):
        cleaned = cleaned[1:-1].strip()
    
    # Remove code fences
    cleaned = re.sub(r"```(?:json)?", "", cleaned).strip()

    # Find JSON segment
    first_bracket = min([i for i in [cleaned.find('['), cleaned.find('{')] if i != -1] or [-1])
    last_bracket = max(cleaned.rfind(']'), cleaned.rfind('}'))
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        segment = cleaned[first_bracket:last_bracket+1]
    else:
        segment = cleaned

    # Fix { [ ... ] } pattern
    if re.fullmatch(r"\{\s*\[.*\]\s*\}", segment, flags=re.DOTALL):
        segment = segment.strip()[1:-1].strip()

    # Try parsing
    data = None
    try:
        data = json.loads(segment)
    except Exception:
        # Try with relaxed quotes
        relaxed = segment
        if '"' not in relaxed and "'" in relaxed:
            relaxed = relaxed.replace("'", '"')
        try:
            data = json.loads(relaxed)
        except Exception:
            try:
                data = ast.literal_eval(segment)
            except Exception:
                pass

    # Extract findings list
    findings_list = []
    if isinstance(data, list):
        findings_list = [str(x).strip() for x in data if str(x).strip()]
    elif isinstance(data, dict):
        for key in ["conditions", "findings", "positive_findings", "PositiveFindings", "list", "data"]:
            if key in data and isinstance(data[key], list):
                findings_list = [str(x).strip() for x in data[key] if str(x).strip()]
                break
        if not findings_list and all(isinstance(v, str) for v in data.values()):
            findings_list = [v.strip() for v in data.values() if v.strip()]

    # Fallback: extract quoted strings
    if not findings_list:
        quoted = re.findall(r'"([^"\\]+(?:\\.[^"\\]*)*)"', segment)
        if quoted:
            findings_list = [q.strip() for q in quoted if q.strip()]

    return findings_list or []


def build_findings_prompt(findings_text):
    """Build prompt for extracting findings."""
    return f"""
You are tasked with an objective.

You will be given a radiology report from a chest X-ray and your goal to report the positive findings present in the report. A "finding" is defined as an abnormality reported by the radiologist. Normal pathologies should not be considered "findings."

EXAMPLE 1:
INPUT: <Normal mediastinum and cardiac silhouette. Smooth convexity along the right upper mediastinum is likely vascular. There is increased air space density both left and right lung base. Persistent elevation left hemidiaphragm. No pneumothorax.>
OUTPUT: <["Smooth convexity along the right upper mediastinum," "increased air space density both left and right lung base", "Persistent elevation left hemidiaphragm"]>

EXAMPLE 2:
INPUT: <deep venous system in both right and left lower extremities from the common femoral vein through the popliteal vein is normally compressible. No echogenic thrombus is seen. There is normal color flow throughout the visualized deep venous system in both legs.,No evidence of deep venous thrombosis in either the right or left lower extremity from the common femoral vein through the popliteal vein.>
OUTPUT: <[]>

Below is the report:
REPORT: <{findings_text}>

PLEASE ONLY OUTPUT A SINGLE LIST, AND NOTHING ELSE.
"""


def extract_findings(nrows, limit, client):
    """Extract positive findings from RexGradient reports."""
    print("\nExtracting positive findings from reports...")
    
    if not Path(REX_METADATA).exists():
        raise SystemExit(f"RexGradient metadata not found: {REX_METADATA}")
    
    # Load metadata
    print(f"  Loading {nrows} rows from metadata...")
    df = pd.read_csv(REX_METADATA, nrows=nrows) if nrows else pd.read_csv(REX_METADATA)
    
    summaries = {}
    for _, item in df.iterrows():
        case_id = item['id']
        summaries[case_id] = {
            "AccessionNumber": item.get("AccessionNumber"),
            "StudyInstanceUid": item.get("StudyInstanceUid"),
            "Findings": item.get("Findings", ""),
        }
    
    items = list(summaries.items())
    if limit:
        items = items[:limit]
    
    print(f"  Processing {len(items)} cases with OpenAI...")
    
    results = []
    for case_id, content in tqdm(items, desc="  Extracting"):
        findings_text = content["Findings"]
        prompt = build_findings_prompt(findings_text)
        
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an AI assistant acting as an X-ray radiologist."},
                    {"role": "user", "content": prompt}
                ]
            )
            raw_output = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"\n  Error processing case {case_id}: {e}")
            raw_output = f"Error: {e}"
        
        positive_findings_list = [] if raw_output.startswith("Error:") else extract_positive_findings(raw_output)
        
        results.append({
            "AccessionNumber": content["AccessionNumber"],
            "StudyInstanceUid": content["StudyInstanceUid"],
            "Findings": content["Findings"],
            "PositiveFindings": json.dumps(positive_findings_list, ensure_ascii=False),
            "PositiveFindingsCount": len(positive_findings_list)
        })
    
    # Save
    output = DATA_DIR / "rex_findings_counts.csv"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(results).to_csv(output, index=False)
    
    # Show distribution
    counts = pd.Series([r['PositiveFindingsCount'] for r in results]).value_counts().sort_index()
    print(f"\n  Positive findings distribution:")
    for count, freq in counts.items():
        print(f"    {count} findings: {freq} reports")
    
    print(f"  Saved {len(results)} rows to {output}")
    return output


def filter_age(input_csv):
    """Filter out child patients (<18 years)."""
    print("\nFiltering child patients...")
    
    if not Path(input_csv).exists():
        raise SystemExit(f"Input not found: {input_csv}")
    
    # Load metadata
    if not Path(TEST_METADATA_JSON).exists():
        print(f"  Warning: Metadata not found: {TEST_METADATA_JSON}")
        print("  Skipping age filter")
        output = DATA_DIR / "rex_adult.csv"
        import shutil
        shutil.copy2(input_csv, output)
        return output
    
    print(f"  Loading metadata...")
    with open(TEST_METADATA_JSON) as f:
        metadata = json.load(f)
    
    # Build adult accession set
    adult_accessions = set()
    for entry in metadata.values():
        acc = entry.get("AccessionNumber")
        age = entry.get("PatientAge")
        if acc and is_adult_years(age):
            adult_accessions.add(acc)
    
    print(f"  Found {len(adult_accessions)} adult accessions")
    
    # Filter CSV
    with open(input_csv) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames
    
    before = len(rows)
    filtered = [r for r in rows if r.get("AccessionNumber") in adult_accessions]
    removed = before - len(filtered)
    
    # Save
    output = DATA_DIR / "rex_adult.csv"
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(filtered)
    
    print(f"  Filtered: {before} rows → {len(filtered)} rows ({removed} removed)")
    print(f"  Saved to {output}")
    return output


def filter_prior_context(input_csv, client):
    """Filter reports referencing prior imaging."""
    print("\nFiltering reports with prior imaging references...")
    
    if not Path(input_csv).exists():
        raise SystemExit(f"Input not found: {input_csv}")
    
    # Load CSV
    with open(input_csv) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "Findings" not in reader.fieldnames:
            raise SystemExit("CSV missing 'Findings' column")
        header = reader.fieldnames
        rows = list(reader)
    
    print(f"  Loaded {len(rows)} rows")
    
    # Classification prompt
    prompt_prefix = (
        "You are a radiology report triage assistant. Examine the Findings text and decide ONLY if it references prior imaging or any information that cannot be identified with the image alone.\n"
        "REMOVE: Text explicitly references a prior exam/imaging comparison.\n"
        "KEEP: No explicit prior/comparison/temporal imaging reference. Ignore clinical history or chronic conditions unless phrased as unchanged/again noted.\n"
        "Respond with exactly KEEP or REMOVE.\n\nFindings: "
    )
    
    cache = {}
    
    def classify(text):
        if not text.strip():
            return True
        if text in cache:
            return cache[text]
        
        message = prompt_prefix + text.strip()
        heuristic_flag = bool(PRIOR_PATTERN.search(text))
        
        for attempt in range(4):
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": message}]
                )
                ans = (resp.choices[0].message.content or "").strip().upper()
                keep = ans.startswith("KEEP")
                # Conservative: if heuristic matches, remove
                if keep and heuristic_flag:
                    keep = False
                cache[text] = keep
                return keep
            except Exception as e:
                wait = 2 ** attempt
                print(f"\n  Error (attempt {attempt+1}): {e}. Retrying in {wait}s...")
                time.sleep(wait)
        
        raise SystemExit("Failed classification after retries")
    
    # Filter
    filtered = []
    kept = removed = 0
    
    for row in tqdm(rows, desc="  Filtering"):
        findings = row.get("Findings", "")
        keep_row = classify(findings)
        
        if keep_row:
            filtered.append(row)
            kept += 1
        else:
            removed += 1
    
    # Save
    output = DATA_DIR / "rex_adult_image_only.csv"
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(filtered)
    
    print(f"  Input: {len(rows)} rows, kept: {kept}, removed: {removed}")
    print(f"  Saved to {output}")
    return output


def sample_cases(input_csv):
    """Sample cases with target distribution."""
    print("\nSampling cases with target distribution...")
    
    if not Path(input_csv).exists():
        raise SystemExit(f"Input not found: {input_csv}")
    
    # Load existing radgame_report.json to exclude
    radgame_report_path = DATA_DIR / "radgame_report.json"
    excluded_study_uids = set()
    if radgame_report_path.exists():
        try:
            with open(radgame_report_path) as f:
                radgame_data = json.load(f)
            for v in radgame_data.values():
                if isinstance(v, dict) and v.get("StudyInstanceUid"):
                    excluded_study_uids.add(v["StudyInstanceUid"])
            print(f"  Loaded {len(excluded_study_uids)} studies to exclude from radgame_report.json")
        except Exception as e:
            print(f"  Warning: could not parse radgame_report.json: {e}")
    
    # Load CSV
    with open(input_csv) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = list(reader.fieldnames or [])
    
    print(f"  Loaded {len(rows)} rows")
    
    # Filter excluded
    if excluded_study_uids:
        before = len(rows)
        rows = [r for r in rows if r.get('StudyInstanceUid') not in excluded_study_uids]
        removed = before - len(rows)
        if removed:
            print(f"  Removed {removed} already in radgame_report.json")
    
    # Belt-and-suspenders age filtering
    meta_path = Path(TEST_METADATA_JSON)
    under18_study_uids = set()
    null_age_study_uids = set()
    
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta_all = json.load(f)
            for k, v in meta_all.items():
                if not isinstance(v, dict):
                    continue
                age_val = v.get('PatientAge')
                suid = v.get('StudyInstanceUid')
                if not suid and '_s' in k:
                    suid = k.split('_s', 1)[1]
                if age_val is None:
                    if suid:
                        null_age_study_uids.add(suid)
                elif is_under_18(age_val):
                    if suid:
                        under18_study_uids.add(suid)
        except Exception:
            pass
    
    if under18_study_uids or null_age_study_uids:
        before = len(rows)
        rows = [r for r in rows if r.get('StudyInstanceUid') not in under18_study_uids 
                and r.get('StudyInstanceUid') not in null_age_study_uids]
        removed = before - len(rows)
        if removed:
            print(f"  Removed {removed} under-18 or null age")
    
    # Bucket by finding count
    buckets = {}
    for r in rows:
        try:
            c = int(r.get("PositiveFindingsCount", ""))
        except ValueError:
            continue
        buckets.setdefault(c, []).append(r)
    
    print(f"  Available per count: {dict((k, len(v)) for k, v in sorted(buckets.items()))}")
    print(f"  Target distribution: {TARGET_DISTRIBUTION}")
    
    # Sample
    import random
    rng = random.Random(SEED)
    chosen = []
    used_ids = {k: set() for k in buckets}
    deficit = 0
    
    # First pass: sample from each bucket
    for c, target in TARGET_DISTRIBUTION.items():
        avail = len(buckets.get(c, []))
        take = min(target, avail)
        if take:
            pick = rng.sample(buckets[c], take)
            for row in pick:
                used_ids[c].add(id(row))
            chosen.extend(pick)
        if avail < target:
            deficit += target - avail
    
    # Second pass: fill deficit from unused
    if deficit > 0:
        pool = []
        for c, rows_c in buckets.items():
            for r in rows_c:
                if id(r) not in used_ids[c]:
                    pool.append(r)
        if pool:
            extra = rng.sample(pool, min(deficit, len(pool)))
            chosen.extend(extra)
    
    if len(chosen) > TOTAL_SAMPLES:
        chosen = chosen[:TOTAL_SAMPLES]
    
    # Show actual distribution
    summary = {}
    for r in chosen:
        c = int(r["PositiveFindingsCount"])
        summary[c] = summary.get(c, 0) + 1
    
    print(f"  Sampled distribution: {dict(sorted(summary.items()))}")
    print(f"  Total sampled: {len(chosen)} (target: {TOTAL_SAMPLES})")
    
    if len(chosen) < TOTAL_SAMPLES:
        print("  Warning: insufficient rows for full target")
    
    # Augment with ImagePath
    if "ImagePath" not in header:
        header.append("ImagePath")
    
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception as e:
            print(f"  Failed to load metadata: {e}")
            meta = {}
        
        # Build map
        uid_map = {}
        for k, v in meta.items():
            if "_s" in k:
                uid = k.split("_s", 1)[1]
                uid_map[uid] = v
        
        matched = 0
        for r in chosen:
            uid = r.get("StudyInstanceUid", "")
            entry = uid_map.get(uid) or {}
            age_val = entry.get('PatientAge') if isinstance(entry, dict) else None
            if age_val is None or is_under_18(age_val):
                continue
            val = entry.get("ImagePath") if isinstance(entry, dict) else None
            if isinstance(val, list):
                r["ImagePath"] = "|".join(str(x) for x in val)
            elif val:
                r["ImagePath"] = str(val)
            else:
                r["ImagePath"] = ""
            if r["ImagePath"]:
                matched += 1
        
        print(f"  Matched ImagePath for {matched}/{len(chosen)} rows")
    
    # Save
    output = DATA_DIR / "sample_rex.csv"
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(chosen)
    
    print(f"  Saved to {output}")
    return output


def main():
    parser = argparse.ArgumentParser(
        description="Generate RadGame Report Dataset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--nrows', type=int, default=1000,
                       help='Number of metadata rows to load (default: 1000)')
    parser.add_argument('--limit', type=int,
                       help='Limit number of cases processed in extraction')
    parser.add_argument('--skip-confirm', action='store_true',
                       help='Skip confirmation prompts')
    
    args = parser.parse_args()
    
    print("="*80)
    print("RadGame Report Dataset Generator")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Data directory: {DATA_DIR}")
    print()
    
    if not args.skip_confirm:
        response = input("Continue? [y/N]: ")
        if response.lower() != 'y':
            print("Aborted.")
            return
    
    try:
        start_time = time.time()
        client = get_openai_client()
        
        # Run pipeline
        output = extract_findings(args.nrows, args.limit, client)
        output = filter_age(output)
        output = filter_prior_context(output, client)
        output = sample_cases(output)
        
        elapsed = time.time() - start_time
        print("\n" + "="*80)
        print(f"✓ Dataset generation complete in {elapsed/60:.1f} minutes")
        print(f"  Final output: {output}")
        print("="*80)
        
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
