import os
import shutil
import sys
from pathlib import Path
from tqdm import tqdm

# recursively copy all PNGs from source to destination
def get_all_pngs(src_dir, dest_dir):
    src_dir = Path(src_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    png_files = []
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.lower().endswith('.png'):
                png_files.append((Path(root) / file, file))

    for src_file, file in tqdm(png_files, desc="Copying PNGs"):
        dest_file = dest_dir / file
        if dest_file.exists():
            print(f"Warning: {dest_file} already exists. Skipping.")
            continue
        shutil.copy2(src_file, dest_file)


def main():
    if len(sys.argv) >= 3:
        src_dir = sys.argv[1]
        dest_dir = sys.argv[2]
    else:
        src_dir = input("Enter the source directory: ")
        dest_dir = input("Enter the destination directory: ")
    get_all_pngs(src_dir, dest_dir)


if __name__ == "__main__":
    main()
