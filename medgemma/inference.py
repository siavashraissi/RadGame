from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import List, Any, Dict, Optional

from PIL import Image, ImageDraw

# optional imports with fallbacks
try:
	import torch
	from transformers import AutoProcessor, AutoModelForImageTextToText
except Exception:
	torch = None
	AutoProcessor = None
	AutoModelForImageTextToText = None

try:
	from tqdm import tqdm
except Exception:
	def tqdm(x, **_):
		return x


@dataclass
class Config:
	image_dir: str
	json_input: str
	json_output: str
	model_id: str
	save_debug_images: bool = False
	debug_image_dir: str = "medgemma/overlay_debug"
	limit: Optional[int] = None
	max_new_tokens: int = 200


def load_data(path: str, limit: Optional[int]) -> List[Dict[str, Any]]:
	with open(path, "r") as f:
		data = json.load(f)
	if limit is not None:
		data = data[:limit]
	return data


def ensure_model(model_id: str):
	if torch is None or AutoProcessor is None:
		raise RuntimeError(
			"torch / transformers not available. Install with: pip install torch transformers tqdm Pillow"
		)
	model = AutoModelForImageTextToText.from_pretrained(
		model_id,
		torch_dtype=torch.bfloat16,
		device_map="auto",
	)
	processor = AutoProcessor.from_pretrained(model_id)
	return model, processor


def explain_findings(cfg: Config) -> None:
	if cfg.save_debug_images:
		os.makedirs(cfg.debug_image_dir, exist_ok=True)

	data = load_data(cfg.json_input, cfg.limit)
	model, processor = ensure_model(cfg.model_id)

	GENERAL_CONDITIONS = {
		"Cardiomegaly",
		"Hilar enlargement",
		"Hyperinflation",
		"Pleural effusion",
		"Pulmonary fibrosis",
		"Pneumothorax",
		"Scoliosis",
	}

	general_explanations = _generate_general_explanations(
		model, processor, GENERAL_CONDITIONS, cfg.max_new_tokens
	)
	print("Finished generating general explanations.")

	existing_results: List[Dict[str, Any]] = []
	processed_ids: set[str] = set()
	if os.path.exists(cfg.json_output):
		try:
			with open(cfg.json_output, "r") as f:
				existing_results = json.load(f)
			for rec in existing_results:
				if isinstance(rec, dict) and rec.get("ImageID"):
					processed_ids.add(rec["ImageID"])
			print(f"Loaded existing output with {len(processed_ids)} processed images; will skip them.")
		except Exception as e:
			print(f"Warning: could not read existing output ({e}); starting fresh.")
			existing_results = []
			processed_ids.clear()

	results = existing_results

	for item in tqdm(data, desc="Images"):
		if not isinstance(item, dict):
			continue
		image_id = item.get("ImageID")
		if not image_id:
			results.append(item)
			try:
				with open(cfg.json_output, "w") as f:
					json.dump(results, f, indent=2)
			except Exception:
				pass
			continue
		if image_id in processed_ids:
			continue

		image_path = os.path.join(cfg.image_dir, image_id)
		item_out = item.copy()
		processed_findings: List[Dict[str, Any]] = []

		image = None
		width, height = 0, 0
		needs_image_load = any(
			not GENERAL_CONDITIONS.intersection(f.get("labels", []))
			for f in item.get("findings", [])
		)

		if needs_image_load and os.path.exists(image_path):
			try:
				image = Image.open(image_path)
				width, height = image.size
			except Exception as e:
				print(f"Error opening image {image_path}: {e}")
		
		elif not os.path.exists(image_path):
			for finding in item.get("findings", []) or []:
				f_copy = finding.copy()
				f_copy["medgemma_explanation"] = "Image file not found."
				processed_findings.append(f_copy)
			item_out["findings"] = processed_findings
			results.append(item_out)
			processed_ids.add(image_id)
			try:
				with open(cfg.json_output, "w") as f:
					json.dump(results, f, indent=2)
			except Exception:
				pass
			continue

		for idx, finding in enumerate(item.get("findings", []) or []):
			f_copy = finding.copy()
			
			finding_labels = set(finding.get("labels", []))
			general_label_match = GENERAL_CONDITIONS.intersection(finding_labels)

			if general_label_match:
				matched_label = next(iter(general_label_match))
				f_copy["medgemma_explanation"] = general_explanations.get(
					matched_label, "General explanation not found."
				)
				processed_findings.append(f_copy)
				continue

			if image is None and needs_image_load:
				f_copy["medgemma_explanation"] = f"Failed to open image: {image_path}"
				processed_findings.append(f_copy)
				continue

			boxes = finding.get("boxes") or []
			if boxes and image:
				box = boxes[0]
				x_min = int(box[0] * width)
				y_min = int(box[1] * height)
				x_max = int(box[2] * width)
				y_max = int(box[3] * height)

				cropped = image.crop((x_min, y_min, x_max, y_max))
				overlaid = image.copy().convert("RGB")
				draw = ImageDraw.Draw(overlaid)
				draw.rectangle([x_min, y_min, x_max, y_max], outline="red", width=10)

				if cfg.save_debug_images:
					label_slug = "_".join(finding.get("labels", ["finding"])) or "finding"
					label_slug = label_slug.replace(" ", "_")
					overlaid.save(
						os.path.join(
							cfg.debug_image_dir,
							f"{os.path.splitext(image_id)[0]}_{idx}_{label_slug}.png",
						)
					)

				labels_joined = ", ".join(finding.get("labels", ["this finding"]))
				locations_joined = ", ".join(finding.get("locations", ["unspecified location"]))
				sentence_en = finding.get("sentence_en", "N/A")
				prompt_text = (
					"You are an expert radiologist analyzing a chest X-ray. "
					"An area of interest is marked with a red box in the first image, and the content of that box is shown in the second image. "
					f"The reported finding is: '{sentence_en}'. "
					f"This corresponds to the label(s) '{labels_joined}' at location(s) '{locations_joined}'. "
					"Describe the key visual features within the bounding box that confirm this finding. "
					"Be concise and refer to the second image as 'the bounding box'. Respond in no more than two sentences."
				)

				messages = [
					{"role": "system", "content": [{"type": "text", "text": "You are an expert radiologist."}]},
					{"role": "user", "content": [
						{"type": "text", "text": prompt_text},
						{"type": "image", "image": overlaid},
						{"type": "image", "image": cropped},
					]},
				]

				try:
					inputs = processor.apply_chat_template(
						messages,
						add_generation_prompt=True,
						tokenize=True,
						return_dict=True,
						return_tensors="pt",
					).to(model.device, dtype=torch.bfloat16)

					input_len = inputs["input_ids"].shape[-1]
					with torch.inference_mode():
						generation = model.generate(
							**inputs, max_new_tokens=cfg.max_new_tokens, do_sample=False
						)
						out_tokens = generation[0][input_len:]
					explanation = processor.decode(out_tokens, skip_special_tokens=True)
				except Exception as e:
					explanation = f"Inference error: {e}"
				f_copy["medgemma_explanation"] = explanation
			else:
				f_copy["medgemma_explanation"] = None

			processed_findings.append(f_copy)

		item_out["findings"] = processed_findings
		results.append(item_out)
		processed_ids.add(image_id)
		try:
			with open(cfg.json_output, "w") as f:
				json.dump(results, f, indent=2)
		except Exception as e:
			print(f"Warning: failed to save intermediate output for {image_id}: {e}")

	print(f"Saved: {cfg.json_output}")


def _generate_general_explanations(
	model, processor, conditions: set[str], max_new_tokens: int
) -> Dict[str, str]:
	explanations: Dict[str, str] = {}
	print(f"Generating general explanations for {len(conditions)} conditions...")
	for condition in tqdm(conditions, desc="General Explanations"):
		prompt = (
			"You are an expert radiologist. "
			f"Provide a general, concise (1-2 sentences) description of the radiological signs of '{condition}' in an X-ray image."
		)
		messages = [
			{"role": "system", "content": [{"type": "text", "text": "You are an expert radiologist."}]},
			{"role": "user", "content": [{"type": "text", "text": prompt}]},
		]
		try:
			inputs = processor.apply_chat_template(
				messages,
				add_generation_prompt=True,
				tokenize=True,
				return_dict=True,
				return_tensors="pt",
			).to(model.device, dtype=torch.bfloat16)
			input_len = inputs["input_ids"].shape[-1]
			with torch.inference_mode():
				generation = model.generate(
					**inputs, max_new_tokens=max_new_tokens, do_sample=False
				)
				out_tokens = generation[0][input_len:]
			explanation = processor.decode(out_tokens, skip_special_tokens=True)
			explanations[condition] = explanation
		except Exception as e:
			explanations[condition] = f"Failed to generate explanation: {e}"
	return explanations


def parse_args() -> Config:
	p = argparse.ArgumentParser(description="Run MedGemma inference to add explanations to findings JSON.")
	p.add_argument("--image_dir", required=True, help="Directory containing images.")
	p.add_argument("--json_input", required=True, help="Path to input JSON (localize_small.json).")
	p.add_argument("--json_output", required=True, help="Path to output JSON.")
	p.add_argument("--model_id", default=os.environ.get("MEDGEMMA_MODEL_ID", "/home/baharoon/models/medgemma-4b-it"), help="Model id/path.")
	p.add_argument("--save_debug_images", action="store_true", help="Save overlaid images for debugging.")
	p.add_argument("--debug_image_dir", default="medgemma/overlay_debug", help="Directory for debug images.")
	p.add_argument("--limit", type=int, default=None, help="Process only first N records.")
	p.add_argument("--max_new_tokens", type=int, default=240, help="Max new tokens for generation.")
	args = p.parse_args()
	return Config(
		image_dir=args.image_dir,
		json_input=args.json_input,
		json_output=args.json_output,
		model_id=args.model_id,
		save_debug_images=args.save_debug_images,
		debug_image_dir=args.debug_image_dir,
		limit=args.limit,
		max_new_tokens=args.max_new_tokens,
	)

def main() -> None:
	cfg = parse_args()
	explain_findings(cfg)

if __name__ == "__main__":  
	main()

