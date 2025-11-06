import openai
import json
import uuid
import os
import random
import time
import sys
from datetime import datetime
from functools import wraps
from secretcodes import OPENAI_API_KEY
from flask import (
    Flask, render_template, send_from_directory, request,
    jsonify, session, redirect, url_for, send_file, abort
)

# label taxonomy - maps various finding names to standard labels
group_mapping = {
    'Atelectasis': 'Atelectasis/Fibrotic band',
    'Fibrotic band': 'Atelectasis/Fibrotic band',
    'Cardiomegaly': 'Cardiomegaly',
    'Consolidation': 'Consolidation',
    'Edema': 'Consolidation',
    'Infiltration': 'Consolidation',
    'Lung Lesion': 'Nodule/Mass',
    'Lung Opacity': 'Consolidation',
    'Nodule': 'Nodule/Mass',
    'Mass': 'Nodule/Mass',
    'Pleural Effusion': 'Pleural effusion',
    'Pleural Other': 'Pleural thickening',
    'Pleural Thickening': 'Pleural thickening',
    'Pneumonia': 'Consolidation',
    'Pneumothorax': 'Pneumothorax',
    'Support Devices': 'Device/Foreign body',
    'Fracture': 'Fracture',
    'Enlarged Cardiomediastinum': 'Cardiomegaly',
    'No Finding': 'OMIT',
    'Scoliosis': 'Scoliosis',
    'Hyperinflation': 'Hyperinflation',
    'Hilar enlargement': 'Hilar enlargement',
    'Device/Foreign body': 'Device/Foreign body',
    'Postoperative change': 'Postoperative change',
    'Increased density': 'Increased density',
    'Hiatal hernia': 'Hiatal hernia',
    'Interstitial pattern': 'Interstitial pattern',
    'Bone density abnormality/lesion': 'Bone density abnormality/lesion',
    'Spinal curvature abnormality': 'Spinal curvature abnormality'
}
from models import db, AccessCode, ActivityLog, Admin, RadgameReportLog, UserCaseLog
import io
from scores.style_score import calculate_style_score
import shortuuid
import pandas as pd

from io import BytesIO
from config import (
    LOCALIZE_JSON,
    REPORT_METADATA_JSON,
    REPORT_IMAGE_BASE,
    LOCALIZE_IMAGE_BASE,
    SHOW_IMAGE_NAME
)
os.environ["RANK"] = "0"
os.environ["WORLD_SIZE"] = "1"
os.environ["MASTER_ADDR"] = "localhost"
os.environ["MASTER_PORT"] = "12355"
from flask_migrate import Migrate
from sqlalchemy import inspect, text
from math import isfinite

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', str(uuid.uuid4()))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///training.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# practice cases needed before post-test unlocks
_default_loc = 375
_default_rep = 150
LOCALIZE_POST_REQUIRED = _default_loc
REPORT_POST_REQUIRED = _default_rep

db.init_app(app)
migrate = Migrate(app, db)

# openai setup
openai.api_key = os.environ.get('OPENAI_API_KEY', OPENAI_API_KEY)
client = openai.OpenAI(api_key=openai.api_key)


# db init and schema updates
with app.app_context():
    db.create_all()
    try:
        inspector = inspect(db.engine)
        access_code_columns = [c['name'] for c in inspector.get_columns('access_codes')]
        if 'localize_mode' not in access_code_columns:
            try:
                db.session.execute(text("ALTER TABLE access_codes ADD COLUMN localize_mode VARCHAR(10) DEFAULT 'active'"))
                db.session.commit()
            except Exception as add_err:
                print(f"[Schema Check] Could not add column localize_mode: {add_err}")
        if 'report_mode' not in access_code_columns:
            try:
                db.session.execute(text("ALTER TABLE access_codes ADD COLUMN report_mode VARCHAR(10) DEFAULT 'active'"))
                db.session.commit()
            except Exception as add_err:
                print(f"[Schema Check] Could not add column report_mode: {add_err}")
        gating_columns = [
            ('took_localize_pre', "ALTER TABLE access_codes ADD COLUMN took_localize_pre BOOLEAN DEFAULT 0"),
            ('took_localize_post', "ALTER TABLE access_codes ADD COLUMN took_localize_post BOOLEAN DEFAULT 0"),
            ('took_report_pre', "ALTER TABLE access_codes ADD COLUMN took_report_pre BOOLEAN DEFAULT 0"),
            ('took_report_post', "ALTER TABLE access_codes ADD COLUMN took_report_post BOOLEAN DEFAULT 0"),
            ('localize_cases_completed', "ALTER TABLE access_codes ADD COLUMN localize_cases_completed INTEGER DEFAULT 0"),
            ('report_cases_completed', "ALTER TABLE access_codes ADD COLUMN report_cases_completed INTEGER DEFAULT 0")
        ]
        for col, stmt in gating_columns:
            if col not in access_code_columns:
                try:
                    db.session.execute(text(stmt))
                    db.session.commit()
                except Exception as add_err:
                    print(f"[Schema Check] Could not add column {col}: {add_err}")
        try:
            ucl_columns = [c['name'] for c in inspector.get_columns('user_case_logs')]
            snapshot_cols = [
                ('localize_cases_completed_snapshot', "ALTER TABLE user_case_logs ADD COLUMN localize_cases_completed_snapshot INTEGER DEFAULT 0")
            ]
            for col, stmt in snapshot_cols:
                if col not in ucl_columns:
                    try:
                        db.session.execute(text(stmt))
                        db.session.commit()
                    except Exception as sc_err:
                        print(f"[Schema Check] Could not add user_case_logs column {col}: {sc_err}")
            try:
                rrl_cols = [c['name'] for c in inspector.get_columns('radgame_report_logs')]
                if 'report_cases_completed_snapshot' not in rrl_cols:
                    try:
                        db.session.execute(text("ALTER TABLE radgame_report_logs ADD COLUMN report_cases_completed_snapshot INTEGER DEFAULT 0"))
                        db.session.commit()
                    except Exception as rrl_err:
                        print(f"[Schema Check] Could not add radgame_report_logs.report_cases_completed_snapshot: {rrl_err}")
            except Exception as inner_rrl_err:
                print(f"[Schema Check] Skipped radgame_report_logs snapshot add: {inner_rrl_err}")
        except Exception as ucl_err:
            print(f"[Schema Check] Skipped user_case_logs snapshot add: {ucl_err}")

    except Exception as schema_err:
        print(f"[Schema Check] Skipped automatic schema add: {schema_err}")
    if not Admin.query.filter_by(username='admin').first():
        admin = Admin(username='admin')
        admin.set_password('admin')
        db.session.add(admin)
        db.session.commit()

RUN_ID = str(uuid.uuid4())


with open(LOCALIZE_JSON) as f:
    _localize_list = json.load(f)

def _clip01(v: float) -> float:
    return max(0.0, min(1.0, v))

def _normalize_boxes_list(boxes):
    norm = []
    for b in (boxes or []):
        try:
            nb = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
            if all(isfinite(v) for v in nb):
                norm.append([_clip01(v) for v in nb])
        except Exception:
            continue
    return norm
_ALLOWED_LABELS = set(v for v in group_mapping.values() if v != 'OMIT')
MERGE_SYNONYMS = {
    'Infiltration': 'Consolidation',
    'Fibrotic band': 'Atelectasis/Fibrotic band',
    'Atelectasis': 'Atelectasis/Fibrotic band'
}
if 'Consolidation' in _ALLOWED_LABELS or 'Atelectasis/Fibrotic band' in _ALLOWED_LABELS:
    _ALLOWED_LABELS.update(MERGE_SYNONYMS.keys())

localize_cases_map = {}
localize_explanations_map = {}
for item in _localize_list:
    img = item.get('ImageID')
    if not img:
        continue
    by_label = {}
    by_label_expl = {}
    for fnd in (item.get('findings') or []):
        norm_boxes = _normalize_boxes_list(fnd.get('boxes'))
        labels = list(filter(None, (fnd.get('labels') or [])))
        if not labels:
            continue
        for lbl in labels:
            if lbl in MERGE_SYNONYMS:
                lbl = MERGE_SYNONYMS[lbl]
            if lbl not in _ALLOWED_LABELS and lbl not in MERGE_SYNONYMS.values():
                continue
            if norm_boxes:
                by_label.setdefault(lbl, []).extend(norm_boxes)
            else:
                by_label.setdefault(lbl, [])
            expl = fnd.get('medgemma_explanation') or None
            if expl:
                by_label_expl.setdefault(lbl, []).append(expl.strip())
    localize_cases_map[img] = by_label
    if by_label_expl:
        localize_explanations_map[img] = by_label_expl

_loc_seen = set()
LOCALIZE_ORDER = []
for item in _localize_list:
    cid = item.get('ImageID')
    if not cid or cid in _loc_seen:
        continue
    if cid in localize_cases_map:
        LOCALIZE_ORDER.append(cid)
        _loc_seen.add(cid)

class_descs = {}

with open(REPORT_METADATA_JSON) as f:
    rexgradient_reports = json.load(f)
    if isinstance(rexgradient_reports, list):
        rexgradient_reports = {str(i): item for i, item in enumerate(rexgradient_reports)}
    print(f"Loaded {len(rexgradient_reports)} rexgradient reports")

# filter reports with short comparison fields
def _is_valid_report_case(case_data):
    if not isinstance(case_data, dict):
        return False
    comparison = case_data.get('Comparison', '')
    return (not comparison) or len(str(comparison)) < 50

REPORT_ORDER = [cid for cid, cdata in rexgradient_reports.items() if _is_valid_report_case(cdata)]

# fetch case by index from ordered lists
def _get_ordered_localize_case(index: int):
    if not LOCALIZE_ORDER:
        return None
    if index < 0 or index >= len(LOCALIZE_ORDER):
        return None
    return LOCALIZE_ORDER[index]

def _get_ordered_report_case(index: int):
    if not REPORT_ORDER:
        return None
    if index < 0 or index >= len(REPORT_ORDER):
        return None
    return REPORT_ORDER[index]


ALL_LABELS = sorted(set(group_mapping.values()) - {'OMIT'})

NON_LOCALIZABLE = {
    'Pneumothorax',
    'Cardiomegaly',
    'Pleural effusion',
    'Hyperinflation',
    'Scoliosis',
    'Hilar enlargement'
}

ALL_LABELS_SET = set(ALL_LABELS)
NON_LOCALIZABLE_SET = set(NON_LOCALIZABLE)
LOCALIZABLE_LABELS = [lbl for lbl in ALL_LABELS if lbl not in NON_LOCALIZABLE_SET]
NON_LOCALIZABLE_LABELS = [lbl for lbl in ALL_LABELS if lbl in NON_LOCALIZABLE_SET]
LOCALIZABLE_LABELS_SET = set(LOCALIZABLE_LABELS)
NONLOCAL_IN_ALL = ALL_LABELS_SET & NON_LOCALIZABLE_SET

LOCALIZE_IMAGE_BASE_ABS = os.path.abspath(LOCALIZE_IMAGE_BASE)

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(LOCALIZE_IMAGE_BASE_ABS, filename)


def generate_access_code(expiration_days=None, localize_mode=None, report_mode=None):
    while True:
        code = shortuuid.ShortUUID().random(length=6).upper()
        if not AccessCode.query.filter_by(code=code).first():
            break
    if localize_mode not in ('active','passive'):
        localize_mode = 'active'
    if report_mode not in ('active','passive'):
        report_mode = 'active'
    report_version = 'guided' if report_mode == 'passive' else 'practice'
    
    access_code = AccessCode(
        code=code,
        report_version=report_version,
        localize_mode=localize_mode,
        report_mode=report_mode
    )
    db.session.add(access_code)
    db.session.commit()
    return code

def bulk_generate_codes(count, expiration_days=None, localize_mode=None, report_mode=None):
    codes = []
    for _ in range(count):
        code = generate_access_code(expiration_days, localize_mode=localize_mode, report_mode=report_mode)
        codes.append(code)
    return codes
def get_analytics_csv():
    codes = AccessCode.query.all()
    data = []
    for code in codes:
        activities = ActivityLog.query.filter_by(
            access_code_id=code.code,
            activity_type='case_completion'
        ).all()
        
        case_activities = len(activities)
        correct_cases = sum(1 for act in activities if act.is_correct)
        
        iou_scores = [act.get_metadata().get('iou_score', 0) for act in activities]
        avg_iou = sum(iou_scores) / len(iou_scores) if iou_scores else 0
        
        total_user_boxes = 0
        total_correct_boxes = 0
        total_images_processed = 0
        total_session_time_ms = 0
        
        for activity in activities:
            metadata = activity.get_metadata()
            boxes = metadata.get('bounding_boxes', {})
            user_boxes = boxes.get('user_submission', [])
            gt_boxes = boxes.get('ground_truth', [])
            total_user_boxes += len(user_boxes)
            total_images_processed += metadata.get('images_processed', 0)
            total_session_time_ms += metadata.get('session_time_ms', 0)
            
            if user_boxes and gt_boxes:
                for user_box in user_boxes:
                    for gt_box in gt_boxes:
                        if user_box['label'] == gt_box['label']:
                            coords1 = user_box['coordinates']
                            coords2 = gt_box['coordinates']
                            x1 = max(coords1[0], coords2[0])
                            y1 = max(coords1[1], coords2[1])
                            x2 = min(coords1[2], coords2[2])
                            y2 = min(coords1[3], coords2[3])
                            
                            if x2 > x1 and y2 > y1:
                                intersection = (x2 - x1) * (y2 - y1)
                                area1 = (coords1[2] - coords1[0]) * (coords1[3] - coords1[1])
                                area2 = (coords2[2] - coords2[0]) * (coords2[3] - coords2[1])
                                union = area1 + area2 - intersection
                                iou = intersection / union if union > 0 else 0
                                
                                if iou > 0.4:
                                    total_correct_boxes += 1
                                    break
        
        avg_session_time_per_case = total_session_time_ms / case_activities if case_activities > 0 else 0
        
        def format_time(ms):
            if ms == 0:
                return "00:00:00"
            hours = int(ms // (1000 * 60 * 60))
            minutes = int((ms % (1000 * 60 * 60)) // (1000 * 60))
            seconds = int((ms % (1000 * 60)) // 1000)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        data.append({
            'code': code.code,
            'status': code.status,
            'created_at': code.created_at,
            'first_login': code.first_login_at,
            'last_login': code.last_login_at,
            'login_attempts': code.login_attempts,
            'total_cases': case_activities,
            'correct_cases': correct_cases,
            'accuracy': f"{(correct_cases / case_activities * 100):.1f}%" if case_activities > 0 else "N/A",
            'avg_iou_score': f"{avg_iou:.3f}",
            'total_boxes_drawn': total_user_boxes,
            'correct_boxes': total_correct_boxes,
            'box_accuracy': f"{(total_correct_boxes / total_user_boxes * 100):.1f}%" if total_user_boxes > 0 else "N/A",
            'total_images_processed': total_images_processed,
            'total_session_time': format_time(total_session_time_ms),
            'avg_session_time_per_case': format_time(avg_session_time_per_case)
        })
    
    df = pd.DataFrame(data)
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    return csv_buffer.getvalue()

def get_detailed_analytics(code=None):
    query = ActivityLog.query.filter_by(activity_type='case_completion')
    if code:
        query = query.filter_by(access_code_id=code)
    activities = query.all()
    detailed_data = []
    
    for activity in activities:
        metadata = activity.get_metadata()
        boxes = metadata.get('bounding_boxes', {})
        
        detailed_data.append({
            'access_code': activity.access_code_id,
            'case_id': activity.case_id,
            'timestamp': activity.timestamp.isoformat(),
            'is_correct': activity.is_correct,
            'iou_score': metadata.get('iou_score'),
            'ground_truth_boxes': boxes.get('ground_truth', []),
            'user_boxes': boxes.get('user_submission', []),
            'images_processed': metadata.get('images_processed', 0),
            'session_time_ms': metadata.get('session_time_ms', 0),
            'session_time_formatted': metadata.get('session_time_formatted', '00:00:00'),
            'current_correct': metadata.get('current_correct', 0),
            'current_incorrect': metadata.get('current_incorrect', 0),
            'total_correct': metadata.get('total_correct', 0),
            'total_incorrect': metadata.get('total_incorrect', 0),
            'total_cases': metadata.get('total_cases', 0),
            'nonlocalizable_selections': metadata.get('nonlocalizable_selections', {}),
            'image_id': metadata.get('image_id', 'unknown')
        })
    
    return detailed_data

def export_detailed_analytics_json(output_path=None):
    activities = ActivityLog.query.filter_by(activity_type='case_completion').all()
    analytics_by_code = {}
    for activity in activities:
        code = activity.access_code_id
        if code not in analytics_by_code:
            analytics_by_code[code] = []
            
        metadata = activity.get_metadata()
        boxes = metadata.get('bounding_boxes', {})
        
        analytics_by_code[code].append({
            'case_id': activity.case_id,
            'timestamp': activity.timestamp.isoformat(),
            'is_correct': activity.is_correct,
            'iou_score': metadata.get('iou_score'),
            'ground_truth_boxes': boxes.get('ground_truth', []),
            'user_boxes': boxes.get('user_submission', []),
            'images_processed': metadata.get('images_processed', 0),
            'session_time_ms': metadata.get('session_time_ms', 0),
            'session_time_formatted': metadata.get('session_time_formatted', '00:00:00'),
            'current_correct': metadata.get('current_correct', 0),
            'current_incorrect': metadata.get('current_incorrect', 0),
            'total_correct': metadata.get('total_correct', 0),
            'total_incorrect': metadata.get('total_incorrect', 0),
            'total_cases': metadata.get('total_cases', 0),
            'nonlocalizable_selections': metadata.get('nonlocalizable_selections', {}),
            'image_id': metadata.get('image_id', 'unknown')
        })
    
    export_data = {
        'export_time': datetime.utcnow().isoformat(),
        'analytics_by_code': analytics_by_code
    }
    
    if output_path:
        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2)
    
    return export_data


def ensure_access_code():
    if 'access_code' not in session:
        default_code = AccessCode.query.filter_by(code='DEFAULT').first()
        if not default_code:
            default_code_str = generate_access_code()
            default_code = AccessCode.query.filter_by(code=default_code_str).first()
            if default_code:
                default_code.code = 'DEFAULT'
                db.session.commit()
            session['access_code'] = 'DEFAULT'
        else:
            session['access_code'] = 'DEFAULT'
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        ensure_access_code()
        return f(*args, **kwargs)
    return decorated_function

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('main_menu'))

@app.route('/selection')
@login_required
def selection():
    return redirect(url_for('main_menu'))

@app.route('/main-menu')
@login_required
def main_menu():
    return render_template('main_menu.html')

# compute IoU between two boxes
def _iou(box1, box2):
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    a1 = max(box1[2] - box1[0], 0) * max(box1[3] - box1[1], 0)
    a2 = max(box2[2] - box2[0], 0) * max(box2[3] - box2[1], 0)
    union = a1 + a2 - inter
    if union <= 0:
        return 0.0
    return inter / union

# score a localization case against ground truth
def _compute_case_scores(image_id, selections, iou_thresh=0.3):
    label_box_map = localize_cases_map.get(image_id, {})
    gt_label_set = set(label_box_map.keys())
    gt_boxes = {lbl: [list(b) for b in (boxes or [])] for lbl, boxes in label_box_map.items() if lbl in LOCALIZABLE_LABELS_SET}
    user_boxes = selections.get('user_boxes', []) or []

    correct = 0
    incorrect = 0
    enriched_boxes = []
    grouped_indices = {}
    for ub in user_boxes:
        lbl = ub.get('label')
        coords = ub.get('coordinates') or []
        if not lbl or not isinstance(coords, (list, tuple)) or len(coords) != 4:
            enriched_boxes.append({'label': lbl, 'coordinates': coords, 'iou': 0.0})
            continue
        try:
            c = [float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3])]
        except Exception:
            c = None
        if not c:
            enriched_boxes.append({'label': lbl, 'coordinates': coords, 'iou': 0.0})
            continue
        idx = len(enriched_boxes)
        enriched_boxes.append({'label': lbl, 'coordinates': c, 'iou': 0.0})
        grouped_indices.setdefault(lbl, []).append(idx)

    for lbl, gts in gt_boxes.items():
        user_idxs = grouped_indices.get(lbl, [])
        used = [False] * len(user_idxs)
        for g in gts:
            best_iou = 0.0
            best_local_idx = -1
            for local_i, box_global_index in enumerate(user_idxs):
                if used[local_i]:
                    continue
                u = enriched_boxes[box_global_index]['coordinates']
                iou = _iou(g, u)
                if iou > best_iou:
                    best_iou = iou
                    best_local_idx = local_i
            if best_local_idx >= 0 and best_iou >= iou_thresh:
                used[best_local_idx] = True
                enriched_boxes[user_idxs[best_local_idx]]['iou'] = float(best_iou)
                correct += 1
            else:
                incorrect += 1
        for local_i, box_global_index in enumerate(user_idxs):
            if not used[local_i]:
                u = enriched_boxes[box_global_index]['coordinates']
                best_any = 0.0
                for g in gts:
                    best_any = max(best_any, _iou(g, u))
                enriched_boxes[box_global_index]['iou'] = float(best_any)
                incorrect += 1

    for lbl, idxs in grouped_indices.items():
        if lbl not in gt_boxes:
            incorrect += len(idxs)

    nonlocal_map = selections.get('nonlocalizable', {}) or {}
    for lbl in NONLOCAL_IN_ALL:
        chosen = bool(nonlocal_map.get(lbl))
        present = lbl in gt_label_set
        if present and chosen:
            correct += 1
        elif present and not chosen:
            incorrect += 1
        elif not present and chosen:
            incorrect += 1

    return int(correct), int(incorrect), enriched_boxes

@app.route('/api/progress/status')
@login_required
def progress_status():
    access = AccessCode.query.filter_by(code=session['access_code']).first()
    if not access:
        return jsonify({'error': 'not found'}), 404
    loc_cases = UserCaseLog.query.filter_by(access_code_id=access.code).count()
    report_cases = RadgameReportLog.query.filter_by(access_code_id=access.code).count()
    access.localize_cases_completed = loc_cases
    access.report_cases_completed = report_cases
    db.session.commit()
    return jsonify({
        'localize_cases_completed': loc_cases,
        'report_cases_completed': report_cases
    })

# snapshot and heartbeat for progress tracking
@app.route('/api/progress/snapshot', methods=['POST'])
@login_required
def progress_snapshot():
    return jsonify({'status': 'ok'})

@app.route('/api/progress/heartbeat', methods=['POST'])
@login_required
def progress_heartbeat():
    return jsonify({'status': 'ok'})
# aggregate user progress stats
@app.route('/api/progress/summary')
@login_required
def progress_summary():
    access = AccessCode.query.filter_by(code=session['access_code']).first()
    if not access:
        return jsonify({'error': 'not found'}), 404
    rows = (
        UserCaseLog.query
        .filter_by(access_code_id=access.code)
        .order_by(UserCaseLog.timestamp.asc())
        .all()
    )

    cases_total = len(rows)
    correct_conditions = sum(int(r.correct_count or 0) for r in rows)
    incorrect_conditions = sum(int(r.incorrect_count or 0) for r in rows)
    total_time_ms = sum(int(r.time_spent_ms or 0) for r in rows)
    last_timer_checkpoint_ms = 0
    if rows:
        try:
            last_timer_checkpoint_ms = max(int(r.timer_checkpoint_ms or 0) for r in rows)
        except Exception:
            last_timer_checkpoint_ms = 0

    def _fmt(ms: int) -> str:
        if not ms:
            return "00:00:00"
        hours = ms // (1000 * 60 * 60)
        minutes = (ms % (1000 * 60 * 60)) // (1000 * 60)
        seconds = (ms % (1000 * 60)) // 1000
        return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"

    return jsonify({
        'cases_total': int(cases_total),
        'correct_cases': int(correct_conditions),
        'incorrect_cases': int(incorrect_conditions),
        'images_total': int(access.localize_cases_completed or 0),
        'total_time_ms': int(total_time_ms),
    'total_time_formatted': _fmt(int(total_time_ms)),
    'last_timer_checkpoint_ms': int(last_timer_checkpoint_ms)
    })

@app.route('/api/report/summary')
@login_required
def report_summary():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    if not access:
        return jsonify({'error': 'not found'}), 404
    from sqlalchemy import func
    rows = RadgameReportLog.query.filter_by(access_code_id=access.code).order_by(RadgameReportLog.timestamp.asc()).all()
    report_count = len(rows)
    avg_green = db.session.query(func.avg(RadgameReportLog.green_score)).filter(
        RadgameReportLog.access_code_id == access.code,
        RadgameReportLog.green_score.isnot(None)
    ).scalar()
    avg_green = float(avg_green) if avg_green is not None else None
    total_time_ms = sum(int(r.time_spent_ms or 0) for r in rows)
    last_timer_checkpoint_ms = 0
    if rows:
        try:
            last_timer_checkpoint_ms = max(int(r.timer_checkpoint_ms or 0) for r in rows)
        except Exception:
            last_timer_checkpoint_ms = 0
    def _fmt(ms: int) -> str:
        if not ms:
            return "00:00:00"
        hours = ms // (1000 * 60 * 60)
        minutes = (ms % (1000 * 60 * 60)) // (1000 * 60)
        seconds = (ms % (1000 * 60)) // 1000
        return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
    return jsonify({
        'report_cases_completed': int(report_count),
        'avg_green_score': avg_green,
        'total_time_ms': int(total_time_ms),
        'total_time_formatted': _fmt(int(total_time_ms)),
        'last_timer_checkpoint_ms': int(last_timer_checkpoint_ms)
    })
@app.route('/')
def index():
    return redirect(url_for('main_menu'))

@app.route('/localize')
@login_required
def localize_practice():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    completed = int(access.localize_cases_completed) if access else 0
    chosen_case = _get_ordered_localize_case(completed)
    if chosen_case is None:
        chosen_case = LOCALIZE_ORDER[-1]
    image_path = chosen_case
    try:
        print(f"[DeterministicLocalize] access={access.code if access else 'NA'} completed={completed} selected={chosen_case}")
    except Exception:
        pass

    label_box_map = localize_cases_map.get(image_path, {})
    explanation_map = localize_explanations_map.get(image_path, {})
    actual = {lbl: ([] if lbl in NON_LOCALIZABLE_SET else list(boxes)) for lbl, boxes in label_box_map.items()}
    detailed_map = {lbl: '' for lbl in label_box_map}
    detailed_names = {lbl: lbl for lbl in label_box_map}
    nonlocalizable_presence = {lbl: (lbl in label_box_map) for lbl in NONLOCAL_IN_ALL}

    if access and getattr(access, 'localize_mode', None) == 'passive':
        return redirect(url_for('localize_guided'))
    return render_template(
        'index.html',
        image_path=image_path,
        image_name=os.path.basename(image_path),
        case_index=(completed + 1),
        total_cases=len(LOCALIZE_ORDER) if LOCALIZE_ORDER else 0,
        localizable_labels=LOCALIZABLE_LABELS,
        non_localizable_labels=NON_LOCALIZABLE_LABELS,
        actual=actual,
        nonlocalizable_presence=nonlocalizable_presence,
        detailed_classes=detailed_map,
        detailed_names=detailed_names,
    medgemma_explanations=explanation_map,
        run_id=RUN_ID,
        access_code=session.get('access_code'),
        show_image_name=SHOW_IMAGE_NAME,
        localize_required=LOCALIZE_POST_REQUIRED
    )

@app.route('/localize-guided')
@login_required
def localize_guided():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    current_index = int(access.localize_cases_completed) if access else 0
    current_case_candidate = _get_ordered_localize_case(current_index)
    if current_case_candidate is None:
        current_case_candidate = LOCALIZE_ORDER[-1] if LOCALIZE_ORDER else ''
    image_path = current_case_candidate
    session['passive_localize_current_case'] = image_path
    session['passive_localize_last_ts'] = time.time()
    label_box_map = localize_cases_map.get(image_path, {})
    actual = {lbl: ([] if lbl in NON_LOCALIZABLE_SET else list(boxes)) for lbl, boxes in label_box_map.items()}
    detailed_map = {lbl: '' for lbl in label_box_map}
    detailed_names = {lbl: lbl for lbl in label_box_map}
    nonlocalizable_presence = {lbl: (lbl in label_box_map) for lbl in NONLOCAL_IN_ALL}
    return render_template(
        'localize_guided.html',
        image_path=image_path,
        image_name=os.path.basename(image_path) if image_path else '',
        localizable_labels=LOCALIZABLE_LABELS,
        non_localizable_labels=NON_LOCALIZABLE_LABELS,
        actual=actual,
        nonlocalizable_presence=nonlocalizable_presence,
        detailed_classes=detailed_map,
        detailed_names=detailed_names,
        run_id=RUN_ID,
        access_code=session.get('access_code'),
        show_image_name=SHOW_IMAGE_NAME
    )

# get next case for guided localization mode
@app.route('/api/localize/guided/next')
@login_required
def api_localize_guided_next():
    if not LOCALIZE_ORDER:
        return jsonify({'error': 'no_cases'}), 404
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    current_case = session.get('passive_localize_current_case')
    if not access:
        if not current_case or current_case not in localize_cases_map:
            current_case = _get_ordered_localize_case(0)
            session['passive_localize_current_case'] = current_case
        session.setdefault('passive_localize_last_ts', time.time())
    else:
        expected_index = int(access.localize_cases_completed or 0)
        expected_case = _get_ordered_localize_case(expected_index)
        if expected_case is None and LOCALIZE_ORDER:
            expected_case = LOCALIZE_ORDER[-1]
        if expected_case:
            current_case = expected_case
            session['passive_localize_current_case'] = current_case
        session.setdefault('passive_localize_last_ts', time.time())
    timer_checkpoint_ms = None
    new_total = None
    try:
        if access and current_case:
            now_ts = time.time()
            last_ts_key = 'passive_localize_last_ts'
            prev_click_ts = session.get(last_ts_key)
            session[last_ts_key] = now_ts
            if isinstance(prev_click_ts, (int, float)) and prev_click_ts > 0:
                delta_ms = int((now_ts - float(prev_click_ts)) * 1000)
                if delta_ms < 0:
                    delta_ms = 0
            else:
                delta_ms = 0

            prev_rows = (
                UserCaseLog.query
                .filter_by(access_code_id=access.code)
                .order_by(UserCaseLog.timestamp.asc())
                .all()
            )
            prev_total_time = sum(int(r.time_spent_ms or 0) for r in prev_rows)
            timer_checkpoint_ms = int(prev_total_time + delta_ms)

            current_val = int(access.localize_cases_completed or 0)
            new_total = current_val + 1
            access.localize_cases_completed = new_total

            log_row = UserCaseLog(
                access_code_id=access.code,
                case_id=str(current_case),
                selections_json='NA',
                time_spent_ms=delta_ms,
                timer_checkpoint_ms=timer_checkpoint_ms,
                correct_count=0,
                incorrect_count=0,
                localize_cases_completed_snapshot=new_total
            )
            db.session.add(log_row)
            db.session.commit()
    except Exception as passive_log_err:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"[PassiveLocalize] Logging current case failed: {passive_log_err}")

    try:
        if access:
            next_case = _get_ordered_localize_case(int(access.localize_cases_completed))
            if next_case is None and LOCALIZE_ORDER:
                next_case = LOCALIZE_ORDER[-1]
        else:
            next_case = current_case
    except Exception:
        next_case = current_case
    session['passive_localize_current_case'] = next_case
    session['passive_localize_last_ts'] = time.time()

    label_box_map = localize_cases_map.get(next_case, {})
    actual = {lbl: ([] if lbl in NON_LOCALIZABLE_SET else list(boxes)) for lbl, boxes in label_box_map.items()}
    detailed_map = {lbl: '' for lbl in label_box_map}
    detailed_names = {lbl: lbl for lbl in label_box_map}
    nonlocalizable_presence = {lbl: (lbl in label_box_map) for lbl in NONLOCAL_IN_ALL}

    return jsonify({
        'image_path': next_case,
        'image_name': os.path.basename(next_case),
        'localizable_labels': LOCALIZABLE_LABELS,
        'non_localizable_labels': NON_LOCALIZABLE_LABELS,
        'actual': actual,
        'nonlocalizable_presence': nonlocalizable_presence,
        'detailed_classes': detailed_map,
        'detailed_names': detailed_names,
        'timer_checkpoint_ms': timer_checkpoint_ms,
        'localize_cases_completed': new_total
    })

@app.route('/report')
@login_required
def report():
    access_code = AccessCode.query.filter_by(code=session['access_code']).first()
    if access_code:
        report_mode = getattr(access_code, 'report_mode', None)
        report_version = getattr(access_code, 'report_version', None)
        is_guided = (report_mode == 'passive') or (report_version == 'guided')
        if is_guided:
            return render_template('report_guided.html', run_id=RUN_ID, access_code=access_code.code)
    return render_template('report.html', run_id=RUN_ID, access_code=access_code.code if access_code else None)

@app.route('/report-guided')
@login_required
def report_guided():
    access_code = AccessCode.query.filter_by(code=session['access_code']).first()
    return render_template('report_guided.html', run_id=RUN_ID, access_code=access_code.code if access_code else None)

@app.route('/api/user/version')
@login_required
def get_user_version():
    access_code = AccessCode.query.filter_by(code=session['access_code']).first()
    if access_code:
        report_mode = getattr(access_code, 'report_mode', None)
        explicit = getattr(access_code, 'report_version', None)
        derived_version = 'guided' if (report_mode == 'passive' or explicit == 'guided') else 'practice'
        return jsonify({'report_version': explicit or derived_version})
    return jsonify({'report_version': 'practice'})

@app.route('/api/user/info')
@login_required
def get_user_info():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    if not access:
        return jsonify({'error': 'not found'}), 404
    lmode = getattr(access, 'localize_mode', None) or 'active'
    rmode = getattr(access, 'report_mode', None) or ('passive' if getattr(access, 'report_version', None) == 'guided' else 'active')
    return jsonify({
        'localize_mode': lmode,
        'report_mode': rmode
    })
# fetch report case with images and metadata
@app.route('/api/report/case')
@login_required
def get_report_case():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    advance = False
    completed = int(getattr(access, 'report_cases_completed', 0)) if access else 0
    requested_case_id = request.args.get('case_id')
    auto_skip_completed = request.args.get('auto_skip_completed') in ('1', 'true', 'True')
    if access:
        practice_cap = REPORT_POST_REQUIRED
        if access.took_report_pre and completed >= practice_cap:
            return jsonify({'error': 'practice_complete', 'total_cases': practice_cap, 'case_index': practice_cap}), 403
    case_index = completed
    case_id = None
    if requested_case_id:
        if requested_case_id in rexgradient_reports:
            case_id = requested_case_id
        else:
            return jsonify({'error': 'Case not found'}), 404
    if not case_id:
        case_id = _get_ordered_report_case(case_index)
    if not case_id:
        return jsonify({'error': 'No valid cases available'}), 404
    case = rexgradient_reports.get(case_id, {})
    
    image_paths = []
    for img_path in case.get('ImagePath', []):
        filename = os.path.basename(img_path)
        if filename:
            image_paths.append(filename)
    
    active_cap = REPORT_POST_REQUIRED
    raw_age = case.get('PatientAge') or case.get('patient_age') or case.get('Age') or ''
    age_val = None
    if isinstance(raw_age, str):
        try:
            age_val = int(''.join([c for c in raw_age if c.isdigit()]) or '0') or None
        except Exception:
            age_val = None
    elif isinstance(raw_age, (int, float)):
        age_val = int(raw_age)
    indication_val = case.get('Indication') or case.get('indication') or ''
    already_completed = False
    next_case_id = None
    try:
        existing_scored = RadgameReportLog.query.filter_by(access_code_id=session.get('access_code'), sample_id=case_id).order_by(RadgameReportLog.timestamp.desc()).first()
        if existing_scored and existing_scored.green_score is not None:
            already_completed = True
            next_case_id = _get_ordered_report_case(completed)
            if auto_skip_completed and next_case_id and case_id != next_case_id:
                case_id = next_case_id
                case = rexgradient_reports.get(case_id, {})
                image_paths = []
                for img_path in case.get('ImagePath', []):
                    filename = os.path.basename(img_path)
                    if filename:
                        image_paths.append(filename)
                existing_scored = RadgameReportLog.query.filter_by(access_code_id=session.get('access_code'), sample_id=case_id).order_by(RadgameReportLog.timestamp.desc()).first()
                already_completed = bool(existing_scored and existing_scored.green_score is not None)
                next_case_id = _get_ordered_report_case(completed) if already_completed else None
    except Exception:
        already_completed = False

    return jsonify({
        'case_id': case_id,
        'images': image_paths,
        'findings': case.get('Findings', ''),
        'impressions': case.get('Impressions', ''),
        'age': age_val,
        'indication': indication_val,
        'case_index': (case_index + 1),
        'total_cases': active_cap,
        'already_completed': already_completed,
        'next_case_id': next_case_id
    })

@app.route('/api/report/guided/log', methods=['POST'])
@login_required
def guided_report_log():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    if not access:
        return jsonify({'error': 'not found'}), 404
    if getattr(access, 'report_mode', None) != 'passive' and getattr(access, 'report_version', None) != 'guided':
        return jsonify({'error': 'not passive'}), 400
    data = request.get_json() or {}
    case_id = data.get('case_id')
    time_spent_ms = int(data.get('time_spent_ms') or 0)
    timer_checkpoint_ms = int(data.get('timer_checkpoint_ms') or time_spent_ms)
    if not case_id:
        return jsonify({'error': 'missing case_id'}), 400
    try:
        current_completed = int(access.report_cases_completed or 0)
        advance_after = bool(data.get('advance_after'))
        if advance_after:
            access.report_cases_completed = current_completed + 1
        log = RadgameReportLog(
            access_code_id=access.code,
            sample_id=str(case_id),
            findings='',
            green_score=None,
            green_score_std=None,
            green_summary=None,
            report_cases_completed_snapshot=int(getattr(access, 'report_cases_completed', 0)),
            time_spent_ms=time_spent_ms,
            timer_checkpoint_ms=timer_checkpoint_ms
        )
        db.session.add(log)
        db.session.commit()
        return jsonify({'ok': True, 'cases_completed': int(access.report_cases_completed or 0)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'db', 'detail': str(e)}), 500
@app.route('/api/debug/ordering')
@login_required
def debug_ordering():
    access = AccessCode.query.filter_by(code=session.get('access_code')).first()
    return jsonify({
        'localize_first5': LOCALIZE_ORDER[:5],
        'report_first5': REPORT_ORDER[:5],
        'localize_completed': int(getattr(access, 'localize_cases_completed', 0)) if access else 0,
        'report_completed': int(getattr(access, 'report_cases_completed', 0)) if access else 0
    })

@app.route('/report/image/<path:filename>')
@login_required
def serve_report_image(filename):
    return send_from_directory(REPORT_IMAGE_BASE, filename)

@app.route('/api/report/submit', methods=['POST'])
@login_required
def submit_report():
    access_code = AccessCode.query.filter_by(code=session['access_code']).first()
    if access_code:
        report_mode = getattr(access_code, 'report_mode', None)
        report_version = getattr(access_code, 'report_version', None)
        if (report_mode == 'passive') or (report_version == 'guided'):
            return jsonify({'error': 'Submissions are disabled in guided mode.'}), 403
    data = request.get_json()
    case_id = data.get('case_id')
    findings = data.get('findings')
    time_spent_ms = int(data.get('time_spent_ms') or 0)

    try:
        existing_log = RadgameReportLog.query.filter_by(access_code_id=session['access_code'], sample_id=case_id).order_by(RadgameReportLog.timestamp.desc()).first()
        if existing_log and existing_log.green_score is not None:
            existing_payload = {}
            try:
                if existing_log.green_summary:
                    existing_payload = json.loads(existing_log.green_summary)
            except Exception:
                existing_payload = {}
            errors_payload = existing_payload.get('errors') or {}
            matched_findings_payload = existing_payload.get('matched_findings') or []
            case_data_cached = rexgradient_reports.get(case_id, {}) if case_id else {}
            return jsonify({
                'green_score': float(existing_log.green_score),
                'summary': existing_payload.get('explanation') or existing_payload.get('Explanation') or '',
                'errors': {
                    'a': list(errors_payload.get('a', []) or []),
                    'b': list(errors_payload.get('b', []) or []),
                    'c': list(errors_payload.get('c', []) or []),
                    'd': list(errors_payload.get('d', []) or []),
                },
                'matched_findings': list(matched_findings_payload),
                'ground_truth': {
                    'findings': case_data_cached.get('Findings', ''),
                    'impressions': case_data_cached.get('Impressions', '')
                },
                'timer_checkpoint_ms': existing_log.timer_checkpoint_ms,
                'duplicate': True
            })
    except Exception:
        pass
    
    case_data = rexgradient_reports.get(case_id)
    if not case_data:
        print(f"[submit_report] Case not found, auto-skipping: {case_id}")
        try:
            access_row = AccessCode.query.filter_by(code=session['access_code']).first()
            current_val = int(access_row.report_cases_completed or 0) if access_row else 0
            prev_rows = RadgameReportLog.query.filter_by(access_code_id=session['access_code']).order_by(RadgameReportLog.timestamp.asc()).all()
            prev_checkpoint = 0
            if prev_rows:
                try:
                    prev_checkpoint = max(int(r.timer_checkpoint_ms or 0) for r in prev_rows)
                except Exception:
                    prev_checkpoint = sum(int(r.time_spent_ms or 0) for r in prev_rows)
            new_checkpoint = int(prev_checkpoint + (time_spent_ms or 0))
            if access_row and current_val < REPORT_POST_REQUIRED:
                access_row.report_cases_completed = current_val + 1
            placeholder_payload = {
                'explanation': 'Auto-skip: case not found. Placeholder entry recorded.',
                'errors': {'a': [], 'b': [], 'c': [], 'd': []},
                'matched_findings': []
            }
            placeholder_log = RadgameReportLog(
                access_code_id=session['access_code'],
                sample_id=str(case_id),
                findings='',
                green_score=1.0,
                green_score_std=0.0,
                green_summary=json.dumps(placeholder_payload),
                report_cases_completed_snapshot=(current_val + 1 if access_row else 0),
                time_spent_ms=time_spent_ms,
                timer_checkpoint_ms=new_checkpoint
            )
            db.session.add(placeholder_log)
            db.session.commit()
            next_case_id = _get_ordered_report_case(int(access_row.report_cases_completed or 0) if access_row else 0)
            return jsonify({
                'skipped': True,
                'reason': 'case_not_found',
                'case_id': case_id,
                'next_case_id': next_case_id,
                'duplicate': False
            })
        except Exception as skip_err:
            db.session.rollback()
            return jsonify({'error': 'case_not_found', 'detail': str(skip_err)}), 500
    
    reference = f"Findings: {case_data.get('Findings', '')}"
    hypothesis = f"Findings: {findings}"
    raw_age = case_data.get('PatientAge') or case_data.get('patient_age') or case_data.get('Age') or ''
    age_val = None
    if isinstance(raw_age, str):
        try:
            age_val = int(''.join([c for c in raw_age if c.isdigit()]) or '0') or None
        except Exception:
            age_val = None
    elif isinstance(raw_age, (int, float)):
        try:
            age_val = int(raw_age)
        except Exception:
            age_val = None
    indication_val = case_data.get('Indication') or case_data.get('indication') or ''
    age_str = str(age_val) if age_val is not None else 'Unknown'
    indication_str = indication_val if indication_val else 'None provided'

    print(reference)
    print(case_id)
    
    try:
        prompt = (f'''
                Objective:

                Evaluate the accuracy of a candidate radiology report in comparison to a reference
                radiology report composed by expert radiologists. Only include positive findings, not normal findings. 
                Do not include notes unrelated to clinical findings. 
                
                Process Overview:
                You will be presented with:
                1. The criteria for making a judgment.
                2. The reference radiology report.
                3. The candidate radiology report.
                4. The desired format for your assessment.
                
                1. Criteria for Judgment:
                For each candidate report, determine only the clinically significant errors.

                Errors can fall into one of these categories:
                    a) False report of a finding in the candidate.
                    b) Missing a finding present in the reference.
                    c) Misidentification of a finding's anatomic location/position.
                    d) Misassessment of the severity of a finding.

                Note: Concentrate on the clinical findings rather than the report's writing style.
                Evaluate only the findings that appear in both reports. 
                
                Patient Context:
                    Age: {age_str}
                    Indication: {indication_str}

                IMPORTANT NOTES: 
                    - Evaluate only positive findings, not normal findings. If a finding is normal, it should not be counted in the errors.
                    - Ignore all references to prior findings and studies. DO NOT COUNT THEM AS ERRORS.
                    - Do NOT penalize the candidate report for omitting specific numeric measurements (e.g., size or dimensions of a nodule/lesion) if the underlying finding is correctly identified. Missing measurements alone is fine since the user writing the candidate report can't measure. They should only be penalized for missing the finding itself.
                    - Do NOT penalize omission of age-appropriate findings that are NOT clinically significant in the context of the indication and patient age.
                    - Do NOT hallucinate or infer findings absent from both reports.
             
                2. Reference Report:
                {reference}
           
                3. Candidate Report:
                {hypothesis}
            
                4. Reporting Your Assessment:
                Format your output as a JSON. Follow this specific format for your output, even if no errors are found:
                ```
                {{
                    "Explanation": "<Explanation>",
                    "ClinicallySignificantErrors": {{
                        "a": ["<Error 1>", "<Error 2>", "...", "<Error n>"],
                        "b": ["<Error 1>", "<Error 2>", "...", "<Error n>"],
                        "c": ["<Error 1>", "<Error 2>", "...", "<Error n>"],
                        "d": ["<Error 1>", "<Error 2>", "...", "<Error n>"]
                    }},
                    "MatchedFindings": ["<Finding 1>", "<Finding 2>", "...", "<Finding n>"]
                }}
                '''
        )

        completion = client.chat.completions.create(
            model="o3",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that provides radiology report grades."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        print(f"OpenAI completion response: {completion.choices[0].message.content}")
        response_data = json.loads(completion.choices[0].message.content)
        summary = response_data.get('Explanation')
        raw_errors = response_data.get('ClinicallySignificantErrors') or {}
        # normalize error buckets to arrays
        ClinicallySignificantErrors = {
            'a': list(raw_errors.get('a', []) or []),
            'b': list(raw_errors.get('b', []) or []),
            'c': list(raw_errors.get('c', []) or []),
            'd': list(raw_errors.get('d', []) or []),
        }
        MatchedFindings = list(response_data.get('MatchedFindings') or [])

        # compute GREEN score from matched findings and errors
        total_matched = len(MatchedFindings)
        total_sig_errors = len(ClinicallySignificantErrors['a']) + \
                            len(ClinicallySignificantErrors['b']) + \
                            len(ClinicallySignificantErrors['c']) + \
                            len(ClinicallySignificantErrors['d'])

        if total_sig_errors == 0:
            green_score = 1.0
        else:
            green_score = total_matched / (total_matched + total_sig_errors) 

        std_score = 1 - green_score  # std_score is the inverse of green_score

        # Create report log entry
        try:
            # Fetch current report cases completed BEFORE increment for snapshot
            access_row = AccessCode.query.filter_by(code=session['access_code']).first()
            pre_increment_total = int(access_row.report_cases_completed or 0) if access_row else 0
            # Block further practice submissions if cap reached or post-test already taken
            if access_row and access_row.took_report_pre and pre_increment_total >= REPORT_POST_REQUIRED:
                return jsonify({'error': 'practice_complete'}), 403
            if access_row and access_row.took_report_post:
                return jsonify({'error': 'post_test_completed'}), 403
            # Determine cumulative checkpoint for continuous timer
            prev_rows = RadgameReportLog.query.filter_by(access_code_id=session['access_code']).order_by(RadgameReportLog.timestamp.asc()).all()
            prev_checkpoint = 0
            if prev_rows:
                try:
                    prev_checkpoint = max(int(r.timer_checkpoint_ms or 0) for r in prev_rows)
                except Exception:
                    prev_checkpoint = sum(int(r.time_spent_ms or 0) for r in prev_rows)
            new_checkpoint = int(prev_checkpoint + (time_spent_ms or 0))
            full_llm_payload = {
                'explanation': summary,
                'errors': ClinicallySignificantErrors,
                'matched_findings': MatchedFindings,
                'raw_model_json': response_data
            }
            report_log = RadgameReportLog(
                access_code_id=session['access_code'],
                sample_id=case_id,
                findings=findings,
                #impression=impressions,
                green_score=float(green_score),
                green_score_std=float(std_score),
                green_summary=json.dumps(full_llm_payload),
                report_cases_completed_snapshot=pre_increment_total + 1,
                time_spent_ms=time_spent_ms,
                timer_checkpoint_ms=new_checkpoint
            )
            
            db.session.add(report_log)
            # Increment practice report counter on access code
            if access_row:
                try:
                    current_val = int(access_row.report_cases_completed or 0)
                except Exception:
                    current_val = 0
                if current_val < REPORT_POST_REQUIRED:
                    access_row.report_cases_completed = current_val + 1
            db.session.commit()
        except Exception as db_error:
            print(f"Database Error: {db_error}")
            db.session.rollback()
            return jsonify({'error': f"Database error: {db_error}"}), 500
        
        # Calculate StyleScore
        style_data = {}
        try:
            style_response, style_score = calculate_style_score(findings, client)
            style_data = {
                'style_score': style_score,
                'systematic_evaluation_score': float(style_response.systematic_evaluation_score),
                'organization_language_score': float(style_response.organization_language_score),
                'systematic_evaluation_recommendation': style_response.systematic_evaluation_recommendation,
                'organization_language_recommendation': style_response.organization_language_recommendation
            }
        except Exception as style_error:
            print(f"StyleScore error: {style_error}")
            # Set default values if StyleScore fails
            style_data = {
                'style_score': 0,
                'systematic_evaluation_score': 0,
                'organization_language_score': 0,
                'systematic_evaluation_recommendation': '',
                'organization_language_recommendation': ''
            }

        return jsonify({
            'green_score': green_score,
            'summary': summary,
            'errors': ClinicallySignificantErrors,
            'matched_findings': MatchedFindings,
            'ground_truth': {
                'findings': case_data.get('Findings', ''),
                'impressions': case_data.get('Impressions', '')
            },
            'timer_checkpoint_ms': report_log.timer_checkpoint_ms,
            'style_data': style_data
        })

    except openai.APIError as e:
        print(f"OpenAI API Error: {e}")
        return jsonify({'error': f"An error occurred with the OpenAI API: {e}"}), 500
    except Exception as e:
        print(f"Error getting GREEN score: {e}")
        return jsonify({'error': f"A server error occurred: {e}"}), 500

@app.route('/test_openai') # openai endpoint works! 
def test_openai():
    try:
        prompt = "Hello, this is a test. Please respond with a short story"
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a test assistant that only returns JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        response_content = completion.choices[0].message.content
        return jsonify({
            'status': 'success',
            'response': json.loads(response_content)
        })
    except Exception as e:
        print(f"OpenAI test error: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# log completed case with scoring
@app.route('/api/complete_case', methods=['POST'])
@login_required
def complete_case():
    # Store a row into user_case_logs each time the user advances to next case
    if not request.is_json:
        return jsonify({'error': 'invalid payload'}), 400
    data = request.get_json() or {}
    case_id = data.get('case_id') or data.get('image_id')
    let_meta = data.get('metadata') or {}
    # Build selections from explicit selections or fallback to metadata-shape used by older clients
    selections = data.get('selections')
    if not selections:
        # metadata.bounding_boxes.user_submission and metadata.nonlocalizable_selections
        bb = (let_meta.get('bounding_boxes') if isinstance(let_meta, dict) else None) or {}
        user_sub = bb.get('user_submission') or []
        if isinstance(user_sub, dict):
            user_sub = []
        nl_map = (let_meta.get('nonlocalizable_selections') if isinstance(let_meta, dict) else None) or {}
        try:
            selections = {
                'localize_selected_labels': list({ (b.get('label') or 'Unknown') for b in (user_sub or []) }),
                'nonlocalizable': nl_map or {},
                'user_boxes': [ { 'label': b.get('label'), 'coordinates': b.get('coordinates') } for b in (user_sub or []) ]
            }
        except Exception:
            selections = { 'localize_selected_labels': [], 'nonlocalizable': {}, 'user_boxes': [] }
    time_spent_ms = int(data.get('time_spent_ms') or (let_meta.get('session_time_ms') if isinstance(let_meta, dict) else 0) or 0)
    # compute counts server-side for consistency
    computed_correct, computed_incorrect, enriched_boxes = _compute_case_scores(case_id, selections)
    # overwrite user_boxes with enriched list containing per-box IoU
    try:
        selections['user_boxes'] = enriched_boxes
    except Exception:
        pass
    # Allow client-provided counts as fallback if compute fails to produce any (edge-case)
    correct_count = int(data.get('correct_count')) if isinstance(data.get('correct_count'), int) else computed_correct
    incorrect_count = int(data.get('incorrect_count')) if isinstance(data.get('incorrect_count'), int) else computed_incorrect
    if not case_id:
        return jsonify({'error': 'case_id is required'}), 400
    # Determine new checkpoint as cumulative time so far (sum of previous + this case's time)
    prev_rows = (
        UserCaseLog.query
        .filter_by(access_code_id=session['access_code'])
        .order_by(UserCaseLog.timestamp.asc())
        .all()
    )
    prev_total_time = sum(int(r.time_spent_ms or 0) for r in prev_rows)
    timer_checkpoint_ms = int(prev_total_time + (time_spent_ms or 0))

    # Fetch access code row early for snapshot values
    access = AccessCode.query.filter_by(code=session['access_code']).first()
    if access:
        next_localize_total = int(access.localize_cases_completed or 0) + 1
        report_total = int(access.report_cases_completed or 0)
    else:
        # Fallbacks if access row missing (should not normally happen)
        next_localize_total = len(prev_rows) + 1
        report_total = 0

    row = UserCaseLog(
        access_code_id=session['access_code'],
        case_id=str(case_id),
        selections_json=json.dumps(selections),
        time_spent_ms=time_spent_ms,
        timer_checkpoint_ms=timer_checkpoint_ms,
        correct_count=correct_count,
        incorrect_count=incorrect_count,
        localize_cases_completed_snapshot=next_localize_total
    )
    db.session.add(row)

    # Persist increment to access code (only if record exists)
    if access:
        access.localize_cases_completed = next_localize_total
    db.session.commit()
    # Return updated summary so UI can refresh banner immediately
    return progress_summary()

@app.route('/api/user_timer_checkpoint', methods=['POST'])
@login_required
def user_timer_checkpoint():
    """Update the timer checkpoint for the most recent case row for this user.
    Body: { timer_checkpoint_ms: int }
    """
    try:
        payload = request.get_json(silent=True) or {}
        value = int(payload.get('timer_checkpoint_ms') or 0)
    except Exception:
        return jsonify({'error': 'invalid timer value'}), 400
    if value < 0:
        value = 0

    # Find the most recent case row for this access code
    row = (
        UserCaseLog.query
        .filter_by(access_code_id=session['access_code'])
        .order_by(UserCaseLog.timestamp.desc())
        .first()
    )
    if not row:
        return jsonify({'error': 'no case found to checkpoint'}), 404
    try:
        row.timer_checkpoint_ms = value
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    return jsonify({'status': 'ok', 'timer_checkpoint_ms': int(row.timer_checkpoint_ms)})

# admin auth decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        admin = Admin.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            session['admin_id'] = admin.id
            return redirect(url_for('admin_dashboard'))
        return render_template('admin/login.html', error='Invalid credentials')
    return render_template('admin/login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin/dashboard.html')

@app.route('/admin/generate_codes', methods=['POST'])
@admin_required
def admin_generate_codes():
    data = request.get_json()
    localize_mode = data.get('localize_mode') or 'active'
    report_mode = data.get('report_mode') or 'active'
    codes = bulk_generate_codes(1, None, localize_mode=localize_mode, report_mode=report_mode)
    return jsonify({'codes': codes, 'localize_mode': localize_mode, 'report_mode': report_mode})

@app.route('/admin/analytics')
@admin_required
def admin_analytics():
    format_type = request.args.get('format', 'json')
    detailed = request.args.get('detailed', 'false').lower() == 'true'
    code = request.args.get('code')
    
    if detailed and code:
        # Return detailed analytics for specific code
        detailed_data = get_detailed_analytics(code)
        return jsonify({'detailed_analytics': detailed_data})
    
    if format_type == 'csv':
        csv_data = get_analytics_csv()
        return send_file(
            io.StringIO(csv_data),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'access_codes_analytics_{datetime.now().strftime("%Y%m%d")}.csv'
        )
    elif format_type == 'json-download':
        # Export detailed JSON
        json_data = export_detailed_analytics_json()
        return send_file(
            io.StringIO(json.dumps(json_data, indent=2)),
            mimetype='application/json',
            as_attachment=True,
            download_name=f'detailed_analytics_{datetime.now().strftime("%Y%m%d")}.json'
        )
    
    # Default JSON: minimal fields used by admin dashboard
    codes = AccessCode.query.all()
    enriched = []
    for ac in codes:
        enriched.append({
            'code': ac.code,
            'localize_mode': getattr(ac, 'localize_mode', 'active'),
            'report_mode': getattr(ac, 'report_mode', 'active'),
            'took_localize_pre': getattr(ac, 'took_localize_pre', False),
            'took_localize_post': getattr(ac, 'took_localize_post', False),
            'took_report_pre': getattr(ac, 'took_report_pre', False),
            'took_report_post': getattr(ac, 'took_report_post', False),
            'localize_cases_completed': int(getattr(ac, 'localize_cases_completed', 0) or 0),
            'report_cases_completed': int(getattr(ac, 'report_cases_completed', 0) or 0)
        })

    return jsonify({'codes': enriched})

@app.route('/admin/export_code_json')
@admin_required
def admin_export_code_json():
    code_value = request.args.get('code')
    if not code_value:
        return jsonify({'error': 'Missing code'}), 400
    try:
        # Ensure schema so queries below don't fail on older DBs
        ensure_test_logs_schema_runtime()

        ac = AccessCode.query.filter_by(code=code_value).first()
        if not ac:
            return jsonify({'error': 'Code not found'}), 404

        # AccessCode base info (explicit, no derived metrics like accuracy/total/correct)
        base = {
            'code': ac.code,
            'status': ac.status,
            'created_at': ac.created_at.isoformat() if ac.created_at else None,
            'first_login_at': ac.first_login_at.isoformat() if ac.first_login_at else None,
            'last_login_at': ac.last_login_at.isoformat() if ac.last_login_at else None,
            'login_attempts': ac.login_attempts,
            'localize_mode': getattr(ac, 'localize_mode', 'active'),
            'report_mode': getattr(ac, 'report_mode', 'active'),
            'report_version': getattr(ac, 'report_version', 'practice'),
            'took_localize_pre': getattr(ac, 'took_localize_pre', False),
            'took_localize_post': getattr(ac, 'took_localize_post', False),
            'took_report_pre': getattr(ac, 'took_report_pre', False),
            'took_report_post': getattr(ac, 'took_report_post', False),
            'localize_cases_completed': getattr(ac, 'localize_cases_completed', 0),
            'report_cases_completed': getattr(ac, 'report_cases_completed', 0)
        }

        # Activities (include all types) - raw metadata only; no derived accuracy fields
        acts = ActivityLog.query.filter_by(access_code_id=code_value).all()
        activities = []
        for a in acts:
            activities.append({
                'id': a.id,
                'type': a.activity_type,
                'case_id': a.case_id,
                'timestamp': a.timestamp.isoformat() if a.timestamp else None,
                'is_correct': a.is_correct,
                'metadata': a.get_metadata()
            })

        # User case logs (localize practice)
        ucls = UserCaseLog.query.filter_by(access_code_id=code_value).all()
        user_case_logs = [u.to_dict() for u in ucls]

        # Report logs
        rls = RadgameReportLog.query.filter_by(access_code_id=code_value).all()
        report_logs = [r.to_dict() for r in rls]

        # Localize and Report test logs
        ltls = LocalizeTestCaseLog.query.filter_by(access_code_id=code_value).all()
        localize_test_case_logs = [l.to_dict() for l in ltls]
        rtls = ReportTestCaseLog.query.filter_by(access_code_id=code_value).all()
        report_test_case_logs = [r.to_dict() for r in rtls]

        export = {
            'export_time': datetime.utcnow().isoformat(),
            'code_summary': base,
            'activities': activities,
            'user_case_logs': user_case_logs,
            'report_logs': report_logs,
            'localize_test_case_logs': localize_test_case_logs,
            'report_test_case_logs': report_test_case_logs
        }

        from flask import Response
        payload = json.dumps(export, indent=2)
        filename = f"radgame_export_{code_value}_{datetime.utcnow().strftime('%Y%m%d')}.json"
        return Response(
            payload,
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/update_code_modes', methods=['POST'])
@admin_required
def admin_update_code_modes():
    data = request.get_json() or {}
    code_value = data.get('code')
    loc_mode = data.get('localize_mode')
    rep_mode = data.get('report_mode')
    if not code_value:
        return jsonify({'error': 'Missing code'}), 400
    if loc_mode not in (None, 'active', 'passive') or rep_mode not in (None, 'active', 'passive'):
        return jsonify({'error': 'Invalid modes'}), 400
    access_code = AccessCode.query.filter_by(code=code_value).first()
    if not access_code:
        return jsonify({'error': 'Code not found'}), 404
    if loc_mode:
        access_code.localize_mode = loc_mode
    if rep_mode:
        access_code.report_mode = rep_mode
        # Maintain legacy report_version for compatibility with existing front-ends
        try:
            access_code.report_version = 'guided' if rep_mode == 'passive' else 'practice'
        except Exception:
            pass
    db.session.commit()
    return jsonify({'status': 'updated', 'code': code_value, 'localize_mode': access_code.localize_mode, 'report_mode': access_code.report_mode})

@app.route('/admin/delete_code', methods=['POST'])
@admin_required
def admin_delete_code():
    data = request.get_json() or {}
    code_value = data.get('code')
    if not code_value:
        return jsonify({'error': 'Missing code'}), 400
    access_code = AccessCode.query.filter_by(code=code_value).first()
    if not access_code:
        return jsonify({'error': 'Code not found'}), 404
    try:
        # Delete dependent logs (reports first, then activities + bounding boxes via relationship cascade if configured)
        from models import ActivityLog, RadgameReportLog, BoundingBoxLog
        # Delete bounding boxes via activity logs
        activities = ActivityLog.query.filter_by(access_code_id=code_value).all()
        for act in activities:
            # bounding boxes removed via session.delete on each
            for bb in act.bounding_boxes:
                db.session.delete(bb)
            db.session.delete(act)
        # Delete report logs
        RadgameReportLog.query.filter_by(access_code_id=code_value).delete()
        db.session.delete(access_code)
        db.session.commit()
        return jsonify({'status': 'deleted', 'code': code_value})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/admin/delete_all_codes', methods=['POST'])
@admin_required
def admin_delete_all_codes():
    try:
        from models import ActivityLog, RadgameReportLog, BoundingBoxLog, AccessCode
        # Delete in order to satisfy FKs
        # Delete bounding boxes
        BoundingBoxLog.query.delete()
        # Delete activity logs
        ActivityLog.query.delete()
        # Delete report logs
        RadgameReportLog.query.delete()
        deleted = AccessCode.query.delete()
        db.session.commit()
        return jsonify({'status': 'all_deleted', 'count': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# Make access_code and run_id available in all templates
@app.context_processor
def inject_globals():
    return {
        'access_code': session.get('access_code'),
    'run_id': RUN_ID,
    'show_image_name': SHOW_IMAGE_NAME
    }

if __name__ == '__main__':
    app.run(debug=True, port=5000)
    # app.run(host='0.0.0.0', debug=True, port=5000)