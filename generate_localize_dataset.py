#!/usr/bin/env python3

import argparse
import json
import shutil
import sys
from pathlib import Path
from random import random, shuffle
from typing import Any, Iterable

# labels to exclude from dataset
BLACKLIST = {"foreign body", "aortic atheromatosis", "aortic elongation"}

# findings that don't need bounding boxes
ALLOWED_EMPTY_BOX_LABELS = {
    "cardiomegaly", "hilar enlargement", "hyperinflation",
    "pleural effusion", "pneumothorax", "scoliosis",
}

# labels to oversample during selection
OVERSAMPLE_LABELS = [
    "atelectasis", "pleural effusion", "consolidation", "interstitial pattern",
    "nodule/mass", "hilar enlargement", "pleural thickening",
    "bronchiectasis", "pneumothorax", "infiltration",
]

# sampling params
SAMPLE_SIZE = 250
MIN_COUNT = 10
BASE_WEIGHT = 1.0
PER_MATCH_BONUS = 5

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "data" / "localize.json"
DEFAULT_FILTERED = SCRIPT_DIR / "data" / "localize_filtered.json"
DEFAULT_SAMPLED = SCRIPT_DIR / "data" / "localize_small.json"

# update this path for your system
DEFAULT_SRC_DIR = Path("<path-to-padchest-gr>/Padchest_GR_files/PadChest_GR")
DEFAULT_DEST_DIR = (SCRIPT_DIR.parent / "local_sampled").resolve()


def normalize_label(val: Any) -> Iterable[str]:
    if val is None:
        return ()
    if isinstance(val, str):
        return (val.lower(),)
    try:
        return tuple(str(x).lower() for x in val)
    except Exception:
        return (str(val).lower(),)


def image_labels(img: dict[str, Any]) -> set[str]:
    labels = set()
    if not isinstance(img, dict):
        return labels
    findings = img.get("findings")
    if not isinstance(findings, list):
        return labels
    for f in findings:
        if not isinstance(f, dict):
            continue
        for lbl in normalize_label(f.get("labels")):
            labels.add(lbl)
    return labels


def filter_findings_for_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Filter findings by removing blacklisted labels."""
    removed = 0
    if not isinstance(entry, dict):
        return entry, removed

    findings = entry.get("findings")
    if isinstance(findings, list):
        new_findings = []
        for f in findings:
            if not isinstance(f, dict):
                new_findings.append(f)
                continue
            labels = normalize_label(f.get("labels"))
            if any(lbl in BLACKLIST for lbl in labels):
                removed += 1
                continue
            new_findings.append(f)
        entry = dict(entry)
        entry["findings"] = new_findings
        if "num_of_findings" in entry:
            entry["num_of_findings"] = len(new_findings)
    return entry, removed


def filter_data(data):
    """Filter blacklisted findings and remove invalid images."""
    print("Filtering findings...")
    total_removed = 0
    images_removed = 0
    boxes_removed = 0
    
    # Filter blacklisted labels and drop images with no findings
    if isinstance(data, list):
        new_data = []
        for entry in data:
            new_entry, removed = filter_findings_for_entry(entry)
            total_removed += removed
            if isinstance(new_entry, dict):
                findings = new_entry.get("findings")
                if isinstance(findings, list) and len(findings) == 0:
                    images_removed += 1
                    continue
                if "findings" not in new_entry:
                    images_removed += 1
                    continue
            new_data.append(new_entry)
    elif isinstance(data, dict):
        new_data = {}
        for k, v in data.items():
            if isinstance(v, dict):
                new_v, removed = filter_findings_for_entry(v)
                total_removed += removed
                findings = new_v.get("findings")
                if isinstance(findings, list) and len(findings) == 0:
                    images_removed += 1
                    continue
                if "findings" not in new_v:
                    images_removed += 1
                    continue
                new_data[k] = new_v
            else:
                new_data[k] = v
    else:
        raise SystemExit("Unsupported JSON structure (expected list or dict)")
    
    # Drop images with disallowed empty boxes
    if isinstance(new_data, list):
        final_data = []
        for entry in new_data:
            if isinstance(entry, dict):
                findings = entry.get("findings")
                if isinstance(findings, list):
                    drop = False
                    for f in findings:
                        if not isinstance(f, dict):
                            continue
                        if f.get("boxes") != []:
                            continue
                        labels = set(normalize_label(f.get("labels")))
                        if not (labels & ALLOWED_EMPTY_BOX_LABELS):
                            drop = True
                            break
                    if drop:
                        boxes_removed += 1
                        continue
            final_data.append(entry)
    elif isinstance(new_data, dict):
        final_data = {}
        for k, v in new_data.items():
            if isinstance(v, dict):
                findings = v.get("findings")
                if isinstance(findings, list):
                    drop = False
                    for f in findings:
                        if not isinstance(f, dict):
                            continue
                        if f.get("boxes") != []:
                            continue
                        labels = set(normalize_label(f.get("labels")))
                        if not (labels & ALLOWED_EMPTY_BOX_LABELS):
                            drop = True
                            break
                    if drop:
                        boxes_removed += 1
                        continue
            final_data[k] = v
    else:
        final_data = new_data
    
    print(f"  Total findings removed: {total_removed}")
    print(f"  Images removed (no findings): {images_removed}")
    print(f"  Images removed (empty boxes): {boxes_removed}")
    print(f"  Images kept: {len(final_data)}")
    
    return final_data


def weighted_sample_no_replacement(items: list[Any], weights: list[float], k: int) -> list[Any]:
    """Sample k items without replacement using weights."""
    items = list(items)
    w = list(weights)
    selected = []
    
    if k >= len(items):
        shuffle(items)
        return items[:k]
    
    for _ in range(k):
        if not items:
            break
        total = sum(w)
        if total <= 0:
            idx = int(random() * len(items))
        else:
            r = random() * total
            upto = 0.0
            idx = 0
            for i, wi in enumerate(w):
                upto += wi
                if r <= upto:
                    idx = i
                    break
        selected.append(items.pop(idx))
        w.pop(idx)
    return selected


def sample_data(images, sample_size, min_count):
    """Sample images with weighted distribution."""
    print(f"\nSampling {sample_size} images...")
    
    # Convert to list
    if isinstance(images, dict):
        images = list(images.values())
    else:
        images = list(images)
    
    # Build label sets
    image_label_list = [image_labels(img) for img in images]
    all_labels = set().union(*image_label_list) if image_label_list else set()
    print(f"  Found {len(all_labels)} unique labels across {len(images)} images")
    
    # Calculate weights
    weights = []
    for lbls in image_label_list:
        matches = sum(1 for L in OVERSAMPLE_LABELS if L in lbls)
        w = BASE_WEIGHT + PER_MATCH_BONUS * matches
        weights.append(max(w, 0.0))
    
    # Find rare labels (<=10 occurrences) - include all
    label_to_idxs: dict[str, list[int]] = {}
    for i, lbls in enumerate(image_label_list):
        for l in lbls:
            label_to_idxs.setdefault(l, []).append(i)
    
    rare_labels = [l for l, idxs in label_to_idxs.items() if len(idxs) <= 10]
    required_idxs = set()
    for l in rare_labels:
        required_idxs.update(label_to_idxs.get(l, []))
    
    required_images = [images[i] for i in sorted(required_idxs)] if required_idxs else []
    if required_images:
        print(f"  Including {len(required_images)} images with rare labels: {sorted(rare_labels)}")
    
    # Remove required images from pool
    pool_images = [img for i, img in enumerate(images) if i not in required_idxs]
    pool_weights = [weights[i] for i in range(len(images)) if i not in required_idxs]
    
    # Sample
    if len(required_images) >= sample_size:
        sampled = required_images
        print(f"  Required images ({len(required_images)}) >= sample size")
    else:
        need = sample_size - len(required_images)
        selected = weighted_sample_no_replacement(pool_images, pool_weights, need)
        sampled = required_images + selected
    
    # Ensure all labels appear at least once
    sampled_labels = set().union(*(image_labels(img) for img in sampled)) if sampled else set()
    missing_labels = sorted(lbl for lbl in all_labels if lbl not in sampled_labels)
    replacements = 0
    
    for miss in missing_labels:
        for idx, lbls in enumerate(image_label_list):
            if miss in lbls:
                candidate = images[idx]
                if candidate in sampled:
                    break
                # Replace a sampled image
                replace_idx = None
                for i, s in enumerate(sampled):
                    s_lbls = image_labels(s)
                    if miss in s_lbls:
                        replace_idx = None
                        break
                    if not any(l in s_lbls for l in OVERSAMPLE_LABELS):
                        replace_idx = i
                        break
                if replace_idx is None:
                    replace_idx = len(sampled) - 1
                sampled[replace_idx] = candidate
                replacements += 1
                break
    
    # Ensure minimum count per label
    sampled_ids = [s.get("ImageID") if isinstance(s, dict) else None for s in sampled]
    required_ids = {images[i].get("ImageID") for i in required_idxs}
    
    label_counts = {}
    for img in sampled:
        if not isinstance(img, dict):
            continue
        for l in image_labels(img):
            label_counts[l] = label_counts.get(l, 0) + 1
    
    under_labels = [l for l, cnt in label_counts.items() if cnt < min_count]
    
    if under_labels:
        print(f"  Adjusting for minimum {min_count} occurrences: {len(under_labels)} labels")
        
        def find_replace_index(protected_ids: set[str]) -> int | None:
            for i, s in enumerate(sampled):
                if not isinstance(s, dict):
                    continue
                sid = s.get("ImageID")
                if sid in protected_ids:
                    continue
                s_lbls = image_labels(s)
                if not s_lbls:
                    return i
                can_remove = True
                for l in s_lbls:
                    if label_counts.get(l, 0) <= min_count:
                        can_remove = False
                        break
                if can_remove:
                    return i
            
            for i, s in enumerate(sampled):
                if not isinstance(s, dict):
                    continue
                sid = s.get("ImageID")
                if sid in protected_ids:
                    continue
                return i
            return None
        
        for lbl in under_labels:
            current = label_counts.get(lbl, 0)
            dataset_idxs = label_to_idxs.get(lbl, [])
            needed = min_count - current
            
            if needed <= 0:
                continue
            
            candidates = [images[i] for i in dataset_idxs 
                         if images[i].get("ImageID") not in sampled_ids]
            
            for cand in candidates:
                if needed <= 0:
                    break
                rep_idx = find_replace_index(required_ids)
                if rep_idx is None:
                    break
                
                replaced = sampled[rep_idx]
                sampled[rep_idx] = cand
                
                replaced_id = replaced.get("ImageID") if isinstance(replaced, dict) else None
                if replaced_id in sampled_ids:
                    sampled_ids.remove(replaced_id)
                cid = cand.get("ImageID") if isinstance(cand, dict) else None
                if cid:
                    sampled_ids.append(cid)
                
                for l2 in image_labels(replaced):
                    label_counts[l2] = max(0, label_counts.get(l2, 1) - 1)
                for l2 in image_labels(cand):
                    label_counts[l2] = label_counts.get(l2, 0) + 1
                
                needed -= 1
    
    # Final label counts
    label_counts = {}
    for img in sampled:
        if not isinstance(img, dict):
            continue
        for l in image_labels(img):
            label_counts[l] = label_counts.get(l, 0) + 1
    
    print(f"  Sampled {len(sampled)} images with {len(label_counts)} labels")
    if replacements:
        print(f"  Replaced {replacements} samples for label coverage")
    
    # Show top frequencies
    if label_counts:
        print(f"\n  Label frequencies:")
        for lbl, cnt in sorted(label_counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"    {lbl}: {cnt}")
    
    return sampled


def copy_images(sampled, manifest_path, src_dir, dest_dir):
    """Copy sampled images from source to destination."""
    print(f"\nCopying images to {dest_dir}...")
    
    if not src_dir.exists():
        print(f"  Source directory not found: {src_dir}")
        print("  Skipping image copy")
        return
    
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    total = 0
    copied = 0
    missing = 0
    
    for item in sampled:
        if not isinstance(item, dict):
            continue
        imgid = item.get("ImageID")
        if not imgid:
            continue
        
        total += 1
        src = src_dir / imgid
        dst = dest_dir / imgid
        
        if src.exists():
            try:
                shutil.copy2(src, dst)
                copied += 1
            except Exception as e:
                print(f"  Failed to copy {imgid}: {e}")
        else:
            missing += 1
    
    # Copy manifest
    try:
        shutil.copy2(manifest_path, dest_dir / manifest_path.name)
    except Exception as e:
        print(f"  Failed to copy manifest: {e}")
    
    print(f"  Copied {copied}/{total} images (missing: {missing})")


def main():
    parser = argparse.ArgumentParser(
        description="Generate RadGame Localization Dataset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--input', type=Path, default=DEFAULT_INPUT,
                       help=f'Input JSON file (default: {DEFAULT_INPUT.name})')
    parser.add_argument('--output', type=Path, default=DEFAULT_SAMPLED,
                       help=f'Output JSON file (default: {DEFAULT_SAMPLED.name})')
    parser.add_argument('--sample-size', type=int, default=SAMPLE_SIZE,
                       help=f'Number of images to sample (default: {SAMPLE_SIZE})')
    parser.add_argument('--min-count', type=int, default=MIN_COUNT,
                       help=f'Minimum occurrences per label (default: {MIN_COUNT})')
    parser.add_argument('--skip-copy', action='store_true',
                       help='Skip copying images to destination')
    parser.add_argument('--src-dir', type=Path, default=DEFAULT_SRC_DIR,
                       help='Source directory for images')
    parser.add_argument('--dest-dir', type=Path, default=DEFAULT_DEST_DIR,
                       help='Destination directory for images')
    
    args = parser.parse_args()
    
    print("="*80)
    print("RadGame Localization Dataset Generator")
    print("="*80)
    print(f"Input:  {args.input}")
    print(f"Output: {args.output}")
    print()
    
    try:
        # Load input
        if not args.input.exists():
            raise SystemExit(f"Input file not found: {args.input}")
        
        with args.input.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Filter
        filtered = filter_data(data)
        
        # Save filtered output
        filtered_path = args.output.parent / "localize_filtered.json"
        filtered_path.parent.mkdir(parents=True, exist_ok=True)
        with filtered_path.open("w", encoding="utf-8") as f:
            json.dump(filtered, f, indent=2, ensure_ascii=False)
        print(f"Filtered file written to: {filtered_path}")
        
        # Sample
        sampled = sample_data(filtered, args.sample_size, args.min_count)
        
        # Save sampled output
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as f:
            json.dump(sampled, f, indent=2, ensure_ascii=False)
        print(f"Sampled file written to: {args.output}")
        
        # Copy images
        if not args.skip_copy:
            copy_images(sampled, args.output, args.src_dir, args.dest_dir)
        else:
            print("\nSkipping image copy (--skip-copy flag)")
        
        print("\n" + "="*80)
        print("✓ Dataset generation complete!")
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
