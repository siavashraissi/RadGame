import pandas as pd
import os
import shutil
import argparse
from tqdm import tqdm
import json

# copy patient images from JSON mapping to organized folders
def main():
    parser = argparse.ArgumentParser(description="Extract images for each patient listed in a CSV using one or more JSON dictionaries.")
    parser.add_argument('--csv', required=True, help='CSV file with id column')
    parser.add_argument('--json', required=True, nargs='+', help='One or more JSON files mapping id to image paths')
    parser.add_argument('--img_src', required=True, help='Source directory containing images')
    parser.add_argument('--img_out', required=True, help='Output directory to copy patient images to')
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    if 'id' not in df.columns:
        raise ValueError('CSV must contain an id column')

    patient_dict = {}
    for json_path in args.json:
        with open(json_path, 'r') as f:
            data = json.load(f)
            patient_dict.update(data)

    os.makedirs(args.img_out, exist_ok=True)
    missing_patients = 0
    missing_images = 0
    for pid in tqdm(df['id'].unique(), desc="Processing patients"):
        entry = patient_dict.get(str(pid))
        if not entry or 'ImagePath' not in entry or 'ImageViewPosition' not in entry:
            missing_patients += 1
            continue
        images_to_copy = []
        for i, img_path in enumerate(entry['ImagePath']):
            if entry['ImageViewPosition'][i] == "LATERAL":
                continue
            src_path = os.path.join(args.img_src, os.path.basename(img_path))
            if os.path.exists(src_path):
                images_to_copy.append((src_path, os.path.basename(img_path)))
            else:
                missing_images += 1
        if images_to_copy:
            patient_folder = os.path.join(args.img_out, str(pid))
            os.makedirs(patient_folder, exist_ok=True)
            for src_path, img_name in images_to_copy:
                dst_path = os.path.join(patient_folder, img_name)
                shutil.copy2(src_path, dst_path)
    print(f"Done. {missing_patients} patients missing in JSON. {missing_images} images missing in source.")

if __name__ == "__main__":
    main() 