import os

# show image filename in UI
SHOW_IMAGE_NAME = True

# path setup
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# data files
LOCALIZE_JSON = os.path.join(DATA_DIR, 'localize_small.json')
REPORT_METADATA_JSON = os.path.join(DATA_DIR, 'radgame_report.json')

# image directories - update these for your system
LOCALIZE_IMAGE_BASE = "path/to/localize/image/base"
REPORT_IMAGE_BASE = "path/to/report/image/base"