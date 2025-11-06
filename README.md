# RadGame

An interactive web application for training medical professionals in radiology report writing and localization of findings on chest X-rays.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Dataset Generation](#dataset-generation)
- [Running the Application](#running-the-application)
- [Finding Classes](#finding-classes)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## RadGame: An AI-Powered Platform for Radiology Education

**RadGame** is an AI-powered, gamified platform designed to teach two core radiology skills: **finding localization** and **report generation**.  
The platform integrates large-scale public datasets and AI-driven feedback to deliver **interactive, scalable, and structured learning experiences** for medical trainees.

### Key Features

- **RadGame Localize:**  
  Trainees identify and localize abnormalities on chest X-rays by drawing bounding boxes or selecting findings.  
  Their annotations are automatically compared against expert radiologist labels from the **PadChest-GR** dataset (de Castro et al., 2025).  
  Visual feedback is provided via **MedGemma 4B**, which generates concise explanations for missed or incorrect findings.

- **RadGame Report:**  
  Trainees compose structured radiology reports based on chest X-rays, patient age, and indication.  
  The system evaluates reports using **CRIMSON**, a context-aware metric adapted from GREEN (Ostmeier et al., 2024), implemented via **GPT-o3**.  
  Feedback includes a quantitative score, categorized error summaries, and a “Style Score” assessing systematic evaluation and report clarity.

- **Performance Gains:**  
  In a multi-institutional study, participants using RadGame achieved a **68% improvement in localization accuracy** and a **31% improvement in report-writing accuracy**, compared to 17% and 4% respectively for traditional passive methods.  
  The gamified, feedback-rich environment also reduced the time spent per case, demonstrating improved diagnostic efficiency.

### Datasets and Code
RadGame builds upon publicly available datasets:
- **PadChest-GR** (de Castro et al., 2025) — chest radiographs with bounding box annotations.  
- **ReXGradient-160K** (Zhang et al., 2025) — paired X-rays and radiologist-written reports.

## Installation

### Prerequisites

- Conda (Anaconda or Miniconda)
- Python 3.11+
- OpenAI API key (for dataset generation only)

### Environment Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd RadGame-Dev
   ```

2. **Create Conda environment**
   ```bash
   conda create -n radgame python=3.11
   conda activate radgame
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up API keys** (for dataset generation)
   
   Create a `secretcodes.py` file in the project root:
   ```python
   OPENAI_API_KEY = "your-openai-api-key-here"
   ```
   
   Or set environment variable:
   ```bash
   export OPENAI_API_KEY="your-openai-api-key-here"
   ```


## Dataset Generation

RadGame uses two types of datasets:

### 1. Report Dataset

Generate the report writing dataset from RexGradient metadata:

```bash
# Activate environment
conda activate radgame

# Generate report dataset (default: 1000 rows)
python generate_report_dataset.py

# Custom options
python generate_report_dataset.py --nrows 500 --skip-confirm
```

**What it does:**
- Extracts positive findings from radiology reports using OpenAI GPT-4o-mini
- Filters out pediatric patients (<18 years)
- Removes reports referencing prior imaging
- Samples 50 (or more) cases with target distribution (0-5 findings per report)

**Output:** `data/sample_rex.csv`

**Setup:** Edit `generate_report_dataset.py` and update these paths to match your system:
```python
REX_METADATA = "<path-to-rexgradient>/metadata/train_metadata.csv"
TEST_METADATA_JSON = "<path-to-rexgradient>/metadata/test_metadata.json"
```

### 2. Localize Dataset

Generate the finding localization dataset from BIMCV-Padchest grounded reports:

```bash
# Activate environment
conda activate radgame

# Generate localize dataset (default: 250 images)
python generate_localize_dataset.py

# Custom options
python generate_localize_dataset.py --sample-size 300 --skip-copy
```

**What it does:**
- Filters out blacklisted findings (foreign body, aortic atheromatosis, etc.)
- Removes images with empty bounding boxes (unless non-localizable)
- Samples images with weighted distribution favoring important findings
- Ensures diverse label representation (minimum 10 occurrences per label)
- Copies images to destination directory

**Output:** 
- `data/localize_small.json` - Sampled dataset manifest
- `../local_sampled/` - Copied image files

**Requirements:**
- Grounded Padchest reports at `data/localize.json`
- Padchest-GR image dataset (update `DEFAULT_SRC_DIR` path in script)

**Setup:** Edit `generate_localize_dataset.py` and update this path to match your system:
```python
DEFAULT_SRC_DIR = Path("<path-to-padchest-gr>/Padchest_GR_files/PadChest_GR")
```

## Running the Application

### Start the Flask Server

```bash
# Activate environment
conda activate radgame

# Run the application
python app.py
```
### Default Port

The app runs on port 5000 by default. To change the port:

```python
# In app.py, modify the last line:
app.run(debug=True, port=8080)  # Use port 8080 instead
```

### Development Mode

The application runs in debug mode by default, which:
- Auto-reloads on code changes
- Provides detailed error messages
- Should be disabled in production

To disable debug mode:
```python
app.run(debug=False)
```

## Finding Classes

RadGame uses the following anatomical finding classes:

### Localizable Classes

| Finding                          | Description                                    |
|----------------------------------|------------------------------------------------|
| Atelectasis                      | Collapsed or airless lung tissue               |
| Bronchiectasis                   | Permanent dilation of bronchi                  |
| Bullas                           | Air-filled spaces in lung parenchyma           |
| Calcification                    | Calcium deposits in tissue                     |
| Catheter                         | Medical tube device                            |
| Consolidation                    | Dense lung tissue (infection/fluid)            |
| Fibrotic band                    | Scar tissue in lungs                           |
| Fracture                         | Broken bone                                    |
| Heart device                     | Cardiac pacemaker/ICD                          |
| Hiatal hernia                    | Stomach protrusion through diaphragm           |
| Interstitial pattern             | Abnormal lung interstitium texture             |
| Infiltration                     | Abnormal substance in lung tissue              |
| Nodule/Mass                      | Round opacity in lungs                         |
| Osteosynthesis/suture material   | Surgical hardware                              |
| Pleural thickening               | Thickened pleural lining                       |
| Postoperative change             | Post-surgical alterations                      |
| Prosthesis/endoprosthesis        | Artificial body part                           |
| Tube                             | Chest tube, ET tube, NG tube                   |

### Non-Localizable Classes

| Finding                | Description                                           |
|------------------------|-------------------------------------------------------|
| Cardiomegaly           | Enlarged heart (global finding)                       |
| Hilar enlargement      | Enlarged lung hilum (diffuse)                         |
| Hyperinflation         | Over-expanded lungs (global)                          |
| Pleural effusion       | Fluid in pleural space (gravity-dependent)            |
| Pulmonary fibrosis     | Lung scarring (diffuse pattern)                       |
| Pneumothorax           | Air in pleural space (can vary)                       |
| Scoliosis              | Spinal curvature (structural)                         |

**Note:** Non-localizable findings cannot be indicated with precise bounding boxes due to their diffuse nature or global presentation.

## Development

### Application Routes

**Main Routes:**
- `/` - Landing page
- `/main_menu` - Task selection menu
- `/report` - Report writing interface
- `/report_guided` - Guided report writing (with hints)
- `/submit_report` - Submit and score report

**Data Routes:**
- `/report_cases` - Get list of report cases
- `/localize_cases` - Get list of localization cases
- `/get_image/<path>` - Serve X-ray images

### Adding New Features

1. **New route**: Add to `app.py`
2. **New template**: Add to `templates/`
3. **New styles**: Add to `static/css/`
4. **New JavaScript**: Add to `static/js/`

### Scoring System

The scoring system evaluates:
- **Report Quality**: Style, completeness, accuracy (see `scores/style_score.py`)
- **Localization Accuracy**: IoU-based bounding box matching (see `make_localize_test_scores.py`)

## Security Note

Always keep `secretcodes.py` out of version control, use environment variables for sensitive data in production, review code for hardcoded credentials before sharing

## Acknowledgements

This work was conducted as part of the Machine Learning for Health (ML4H) 2025 proceedings. We thank the participating institutions and study volunteers for their contributions, and the creators of PadChest-GR and ReXGradient-160K datasets for enabling this research. We also acknowledge the developers of MedGemma 4B, GPT-o3, and the GREEN metric, whose tools and frameworks informed RadGame’s design and evaluation.

**Note**: This application is for educational purposes only and should not be used for clinical decision-making

## References

- de Castro, D.C., Bustos, A., Bannur, S., Hyland, S.L., Bouzid, K., Wetscherek, M.T., et al. *PadChest-GR: A bilingual chest X-ray dataset for grounded radiology report generation.* **NEJM AI**, 2(7):AIdbp2401120, 2025.  
- Zhang, X., Acosta, J.N., Miller, J., Huang, O., Rajpurkar, P. *ReXGradient-160K: A large-scale publicly available dataset of chest radiographs with free-text reports.* arXiv:2505.00228, 2025.  
- Ostmeier, S., Xu, J., Chen, Z., Varma, M., Blankemeier, L., et al. *GREEN: Generative Radiology Report Evaluation and Error Notation.* In *Findings of ACL: EMNLP 2024*, pp. 374–390, 2024.  
- Sellergren, A., Kazemzadeh, S., Jaroensri, T., Kiraly, A., et al. *MedGemma Technical Report.* arXiv:2507.05201, 2025.

## Bibtex
Please cite **RadGame** whenever you use it.
```
@article{baharoon2025radgame,
  title={RadGame: An AI-Powered Platform for Radiology Education},
  author={Baharoon, Mohammed and Raissi, Siavash and Jun, John S and Heintz, Thibault and Alabbad, Mahmoud and Alburkani, Ali and Kim, Sung Eun and Kleinschmidt, Kent and Alhumaydhi, Abdulrahman O and Alghamdi, Mohannad Mohammed G and others},
  journal={arXiv preprint arXiv:2509.13270},
  year={2025}
}
```