import argparse
import json
import os

import cv2


def analyze_image(image_path):
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Could not read image: {image_path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Simple threshold and contour detection to find UI elements
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    elements = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        # Filter small regions
        if w * h > 500:
            elements.append({
                "bbox": [int(x), int(y), int(w), int(h)]
            })

    return elements


def main(input_folder, output_file):
    results = {}
    for file_name in sorted(os.listdir(input_folder)):
        if file_name.lower().endswith(('.png', '.jpg', '.jpeg')):
            path = os.path.join(input_folder, file_name)
            elements = analyze_image(path)
            results[file_name] = elements

    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"Saved analysis results to {output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze frames for UI elements")
    parser.add_argument(
        "--input-folder", required=True, dest="input_folder", help="Folder with extracted frames"
    )
    parser.add_argument(
        "--output-file", required=True, dest="output_file", help="Output JSON file path"
    )
    args = parser.parse_args()

    main(args.input_folder, args.output_file)
