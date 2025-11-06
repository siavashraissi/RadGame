import json
import os
from glob import glob
from typing import Dict, List, Tuple

# IoU threshold for box matching
IOU_THRESHOLD = 0.25

# labels that should be treated as equivalent
EQUIVALENT_LABEL_GROUPS = [
    {"Consolidation", "Infiltration", "Infiltriation"},
    {"Fibrotic band", "Atelectasis"},
]

# map each label to its canonical form
LABEL_CANON = {}
for group in EQUIVALENT_LABEL_GROUPS:
    rep = sorted(group)[0]
    for lbl in group:
        LABEL_CANON[lbl.lower()] = rep

def canonical_label(label: str) -> str:
    if not label:
        return label
    return LABEL_CANON.get(label.lower(), label)

GROUND_TRUTH_PATH = os.path.join("data", "localize_test_new.json")
PARTICIPANT_GLOB = os.path.join("data", "participant_jsons", "radgame_export_*.json")
OUTPUT_PATH = os.path.join("data", "localize_test_scores.json")


def iou(box_a: List[float], box_b: List[float]) -> float:
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter_w = max(0.0, x2 - x1)
    inter_h = max(0.0, y2 - y1)
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    area_a = max(0.0, (box_a[2] - box_a[0])) * max(0.0, (box_a[3] - box_a[1]))
    area_b = max(0.0, (box_b[2] - box_b[0])) * max(0.0, (box_b[3] - box_b[1]))
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def load_ground_truth(path: str):
    with open(path, 'r') as f:
        data = json.load(f)
    gt_index: Dict[Tuple[str, str], Dict] = {}
    for entry in data.get("localize_test_case_logs", []):
        key = (entry.get("image_id"), entry.get("test_type"))
        gt_boxes = [
            {"label": b.get("label"), "coordinates": b.get("coordinates")}
            for b in entry.get("user_boxes", [])
            if b.get("coordinates") and b.get("label")
        ]
        gt_nonloc = entry.get("nonlocalizable", {}) or {}
        gt_index[key] = {"boxes": gt_boxes, "nonlocalizable": gt_nonloc, "case_index": entry.get("case_index")}
    return gt_index


def labels_match(user_label: str, gt_label: str, case_index: int) -> bool:
    if not user_label or not gt_label:
        return False
    u = canonical_label(user_label)
    g = canonical_label(gt_label)
    if u == g:
        return True
    if case_index == 23:
        if g == "Consolidation" and user_label in {"Nodule", "Nodule/Mass"}:
            return True
    return False


def match_boxes(gt_boxes, user_boxes, case_index: int):
    gt_by_label = {}
    for g in gt_boxes:
        lbl = canonical_label(g.get("label"))
        gt_by_label.setdefault(lbl, []).append(g)
    user_by_label = {}
    for u in user_boxes:
        ulab = u.get("label")
        coords = u.get("coordinates")
        if not ulab or not coords:
            continue
        can = canonical_label(ulab)
        user_by_label.setdefault(can, []).append(u)

    correct = 0
    incorrect = 0

    for gt_label, gt_list in gt_by_label.items():
        user_candidates = []
        for u in user_boxes:
            if labels_match(u.get("label"), gt_label, case_index):
                user_candidates.append(u)
        matched = False
        if user_candidates:
            for g in gt_list:
                g_coords = g.get("coordinates")
                for u in user_candidates:
                    val = iou(g_coords, u.get("coordinates"))
                    if val >= IOU_THRESHOLD:
                        matched = True
                        break
                if matched:
                    break
        if matched:
            correct += 1
        else:
            incorrect += 1

    for user_label in set(canonical_label(u.get("label")) for u in user_boxes if u.get("label")):
        if user_label in gt_by_label:
            continue
        incorrect += 1

    return correct, incorrect


def score_nonlocalizable(gt_nonloc: Dict[str, bool], user_nonloc: Dict[str, bool]):
    correct = 0
    incorrect = 0
    for label, present in gt_nonloc.items():
        if present:
            if user_nonloc.get(label):
                correct += 1
            else:
                incorrect += 1
    for label, val in (user_nonloc or {}).items():
        if val and not gt_nonloc.get(label):
            incorrect += 1
    return correct, incorrect


def process_participant(file_path: str, gt_index):
    with open(file_path, 'r') as f:
        pdata = json.load(f)
    code = pdata.get("code_summary", {}).get("code") or os.path.basename(file_path).split('_')[2]
    results = {"pre": {"images": {}, "total_correct": 0, "total_incorrect": 0},
               "post": {"images": {}, "total_correct": 0, "total_incorrect": 0}}
    for entry in pdata.get("localize_test_case_logs", []):
        image_id = entry.get("image_id")
        test_type = entry.get("test_type")
        if test_type not in ("pre", "post"):
            continue
        gt = gt_index.get((image_id, test_type))
        if not gt and test_type == "post":
            gt = gt_index.get((image_id, "pre"))
        if not gt:
            continue
        user_boxes = entry.get("user_boxes", [])
        user_nonloc = entry.get("nonlocalizable", {}) or {}
        case_index = gt.get("case_index") if isinstance(gt, dict) else None
        gt_boxes = gt.get("boxes") if isinstance(gt, dict) else []
        if gt_boxes is None:
            gt_boxes = []
        if user_boxes is None:
            user_boxes = []
        try:
            box_correct, box_incorrect = match_boxes(gt_boxes, user_boxes, case_index)
        except Exception as e:
            print(f"DEBUG match_boxes error code={code} image={image_id} test={test_type}: {e}")
            continue
        nl_correct, nl_incorrect = score_nonlocalizable(gt["nonlocalizable"], user_nonloc)
        img_correct = box_correct + nl_correct
        img_incorrect = box_incorrect + nl_incorrect
        results[test_type]["images"][image_id] = {"correct": img_correct, "incorrect": img_incorrect}
        results[test_type]["total_correct"] += img_correct
        results[test_type]["total_incorrect"] += img_incorrect
    for tt in ["pre", "post"]:
        if not results[tt]["images"]:
            results[tt] = None
    return code, results


def main():
    if not os.path.exists(GROUND_TRUTH_PATH):
        raise SystemExit(f"Ground truth file not found: {GROUND_TRUTH_PATH}")
    gt_index = load_ground_truth(GROUND_TRUTH_PATH)
    output_list = []
    for pfile in sorted(glob(PARTICIPANT_GLOB)):
        try:
            code, res = process_participant(pfile, gt_index)
            output_list.append({code: res})
        except Exception as e:
            print(f"Error processing {pfile}: {e}")
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(output_list, f, indent=2)
    print(f"Wrote {OUTPUT_PATH} with {len(output_list)} participants")


if __name__ == "__main__":
    main()
