#@output String status
"""Gel recipe: compose three independently runnable assay-level Jython scripts.

Set GEL_CONFIG or GEL_CONFIG_PATH. Set GEL_WORKFLOW_SOURCE_PATH or GEL_SCRIPT_DIR
when submitting script text through MCP so companion assays can be located.
The legacy per-gel configuration schema is retained; each assay receives its own
configuration and records independently reusable intermediate artifacts.
"""
import csv
import hashlib
import json
import math
import numbers
import os
import sys

import runpy

MODELS = ("same_shape", "shared_width", "product_width_variable")

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    with open(path, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)


def save_csv(path, fields, records):
    handle = open(path, "wb") if sys.version_info[0] == 2 else open(path, "w", newline="")
    try:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(records)
    finally:
        handle.close()


def interval(bounds, limit, label):
    if (len(bounds) != 2 or any(isinstance(v, bool) or not isinstance(v, numbers.Integral) for v in bounds)
            or not 0 <= bounds[0] < bounds[1] <= limit):
        raise ValueError("Invalid %s: %s; expected [start, end) within %s" % (label, bounds, limit))


def validate_config(config, width, height, bit_depth):
    if bit_depth not in (8, 16):
        raise ValueError("Use a single-plane unsigned 8-bit or 16-bit quantitative source")
    if config.get("signal_polarity") not in ("high", "low"):
        raise ValueError("Set signal_polarity to high or low from raw pixel values")
    if not isinstance(config.get("flip_horizontal", False), bool):
        raise ValueError("flip_horizontal must be a boolean")
    crop = config["crop_original_xywh"]
    if len(crop) != 4:
        raise ValueError("crop_original_xywh requires four integers")
    x, y, w, h = crop
    interval([x, x + w], width, "crop x")
    interval([y, y + h], height, "crop y")
    display = config["display_range"]
    if len(display) != 2 or not 0 <= display[0] < display[1] <= 2 ** bit_depth - 1:
        raise ValueError("display_range must be increasing within the raw pixel range")
    top, bottom = config["background_y"]["top"], config["background_y"]["bottom"]
    interval(top, height, "background top")
    interval(bottom, height, "background bottom")
    if top[1] > bottom[0]:
        raise ValueError("Background windows must be separate and ordered")
    if not config.get("lanes"):
        raise ValueError("Select at least one lane")
    names = [lane["name"] for lane in config["lanes"]]
    if len(set(names)) != len(names):
        raise ValueError("Lane names must be unique")
    if config.get("relative_to_lane", names[0]) not in names:
        raise ValueError("relative_to_lane must name a measured lane")
    for lane in config["lanes"]:
        interval(lane["x"], width, "lane x")
        interval(lane["peak_y"], height, "peak y")
        if not (x <= lane["x"][0] < lane["x"][1] <= x + w
                and y <= top[0] and bottom[1] <= y + h
                and top[1] <= lane["peak_y"][0] < lane["peak_y"][1] <= bottom[0]):
            raise ValueError("Lane and background ROIs must fit the crop; backgrounds must flank the peak")
    for excluded in config.get("excluded_regions", []):
        ex, ey, ew, eh = excluded["original_xywh"]
        interval([ex, ex + ew], width, "excluded x")
        interval([ey, ey + eh], height, "excluded y")
        if not (x <= ex and ex + ew <= x + w and y <= ey and ey + eh <= y + h):
            raise ValueError("Excluded region must be retained inside the crop")
    fit = config.get("conversion")
    if not fit:
        return
    assumptions = fit.get("confirmed_assumptions", {})
    if not (assumptions.get("pure_substrate_reference") is True
            and assumptions.get("equal_signal_per_molecule") is True):
        raise ValueError("Conversion requires confirmed pure reference and equal signal per molecule")
    interval(fit["fit_y"], height, "fit y")
    interval(fit["reference_support_y"], height, "reference support y")
    if math.isnan(fit["reference_center_y"]) or math.isinf(fit["reference_center_y"]):
        raise ValueError("reference_center_y must be finite")
    if not fit["models"] or any(model not in MODELS for model in fit["models"]):
        raise ValueError("Unknown or empty reference-shape model list")
    for key in ("product_shifts_px", "substrate_shifts_px", "width_scales", "background_shifts_px"):
        values = fit[key]
        if not values or any(math.isnan(v) or math.isinf(v) for v in values) or len(set(values)) != len(values):
            raise ValueError("Fit grids must contain unique finite values: " + key)
    if min(fit["width_scales"]) <= 0:
        raise ValueError("Width scales must be positive")
    for shift in fit["background_shifts_px"]:
        interval([top[0] + shift, top[1] + shift], height, "shifted background top")
        interval([bottom[0] + shift, bottom[1] + shift], height, "shifted background bottom")
    variants = fit["variants"]
    if not variants or len(set(v["name"] for v in variants)) != len(variants):
        raise ValueError("Fit variants must have unique names")
    for variant in variants:
        interval(variant["reference_x"], width, "reference x")
        interval(variant["mixture_x"], width, "mixture x")
    primary = fit["primary"]
    if (primary["variant"] not in [v["name"] for v in variants]
            or primary["model"] not in fit["models"]
            or primary["background_shift_px"] not in fit["background_shifts_px"]):
        raise ValueError("Primary fit must be one of the sensitivity settings")


def build_assay_configs(config, image_width):
    output = config["output_dir"]
    ox, oy, cw, ch = config["crop_original_xywh"]
    flipped = config.get("flip_horizontal", False)
    fx = image_width - ox - cw if flipped else ox
    operations = ([{"op": "flip", "axis": "horizontal"}] if flipped else [])
    operations.append({"op": "crop", "xywh": [fx, oy, cw, ch]})
    image = {"input_path": config["input_path"], "output_dir": os.path.join(output, "image"),
             "operations": operations,
             "exports": [{"filename": "gel_prepared_raw.tif", "format": "tiff", "mode": "native"},
                         {"filename": "gel_prepared_preview.png", "format": "png", "mode": "display",
                          "display_range": config["display_range"], "dark_signal": config["signal_polarity"] == "high"}]}
    quant = {key: config[key] for key in ("input_path", "signal_polarity", "background_y", "display_range")}
    quant["output_dir"] = os.path.join(output, "quantification")
    quant["lanes"] = [dict(lane) for lane in config["lanes"]]
    quant["relative_to_lane"] = config.get("relative_to_lane", config["lanes"][0]["name"])
    conversion = None
    if config.get("conversion"):
        conversion = dict(config["conversion"])
        conversion["output_dir"] = os.path.join(output, "conversion")
        conversion["profiles_path"] = os.path.join(quant["output_dir"], "profile_data.json")
        quant["background_shifts_px"] = conversion["background_shifts_px"]
        quant["primary_background_shift_px"] = 0
        conversion["variants"] = []
        for variant in config["conversion"]["variants"]:
            pair = {"name": variant["name"]}
            for role in ("reference", "mixture"):
                name = variant["name"] + "__" + role
                quant["lanes"].append({"name": name, "x": variant[role + "_x"]})
                pair[role + "_lane"] = name
            conversion["variants"].append(pair)
        windows = [conversion["fit_y"], conversion["reference_support_y"],
                   [quant["background_y"]["top"][0] + min(quant["background_shifts_px"]),
                    quant["background_y"]["bottom"][1] + max(quant["background_shifts_px"])]]
        quant["profile_y"] = [min(v[0] for v in windows), max(v[1] for v in windows)]
    return image, quant, conversion


def build_peak_decomposition(config, fit_config, profile_data):
    # Translate this gel recipe's lane/background layout into a generic 1-D data contract.
    profiles = {(p["lane"], p["background_shift_px"]): p for p in profile_data["profiles"]}
    first = profile_data["profiles"][0]
    x = list(range(first["y_start"], first["y_start"] + len(first["net_sum_AU_per_row"])))
    datasets = []
    for variant in fit_config["variants"]:
        for shift in fit_config["background_shifts_px"]:
            reference = profiles[(variant["reference_lane"], shift)]
            observed = profiles[(variant["mixture_lane"], shift)]
            if any(p["y_start"] != x[0] or len(p["net_sum_AU_per_row"]) != len(x) for p in (reference, observed)):
                raise ValueError("Gel profiles must share a common row axis")
            datasets.append({"name": variant["name"] + "__bg_" + str(shift),
                             "observed": observed["net_sum_AU_per_row"],
                             "references": {"reference": reference["net_sum_AU_per_row"]},
                             "metadata": {"variant": variant["name"], "background_shift_px": shift}})
    data = {"schema": "image-analysis.peak-curves.v1", "x": x, "x_unit": "px", "signal_unit": "AU / row",
            "source_sha256": profile_data["source_sha256"], "datasets": datasets}
    components = [{"name": name, "reference": "reference", "support": fit_config["reference_support_y"],
                   "center": fit_config["reference_center_y"], "shifts": fit_config[key],
                   "negative_reference_policy": "clip"}
                  for name, key in (("peak_A", "product_shifts_px"), ("peak_B", "substrate_shifts_px"))]
    models = []
    for model in fit_config["models"]:
        widths = [1.0] if model == "same_shape" else fit_config["width_scales"]
        models.append({"name": "first_component_width_variable" if model == "product_width_variable" else model,
                       "width_sets": [{"peak_A": w, "peak_B": w if model == "shared_width" else 1.0} for w in widths]})
    primary = fit_config["primary"]
    peak_config = {"curves_path": os.path.join(config["output_dir"], "peak_curves.json"),
                   "output_dir": os.path.join(config["output_dir"], "decomposition"),
                   "fit_range": fit_config["fit_y"], "components": components, "models": models,
                   "primary": {"dataset": primary["variant"] + "__bg_" + str(primary["background_shift_px"]),
                               "model": "first_component_width_variable" if primary["model"] == "product_width_variable" else primary["model"]},
                   "plot": {"signal_divisor": 1e6, "x_label": "Original vertical position (px)",
                            "signal_label": "Net signal (10^6 AU / row)"}}
    return peak_config, data


def interpret_conversion(decomposition, fit_config):
    # Equal molecular response and reference purity are confirmed by validate_config.
    primary = decomposition["primary"]
    by_name = {v["name"]: v for v in primary["components"]}
    product, substrate = by_name["peak_A"], by_name["peak_B"]
    a, b = product["area"], substrate["area"]
    result = {"product_area_AU_px": a, "substrate_area_AU_px": b,
              "product_fraction": a / (a + b), "substrate_fraction": b / (a + b),
              "product_substrate_ratio": a / b if b > 0 else None,
              "product_shift_px": product["shift"], "substrate_shift_px": substrate["shift"],
              "product_width_scale": product["width_scale"], "substrate_width_scale": substrate["width_scale"],
              "product_template_fraction_in_fit": product["template_fraction_in_fit"],
              "substrate_template_fraction_in_fit": substrate["template_fraction_in_fit"],
              "observed_net_area_AU_px": primary["observed_area_in_fit"],
              "sum_residual_area_AU_px": primary["residual_area"],
              "variant": primary["metadata"]["variant"], "background_shift_px": primary["metadata"]["background_shift_px"],
              "model": fit_config["primary"]["model"],
              "SSE": primary["SSE"], "RMSE": primary["RMSE"], "R_squared": primary["R_squared"],
              "grid_boundary_parameters": primary["grid_boundary_parameters"], "warnings": primary["warnings"]}
    return {"primary": result, "confirmed_assumptions": fit_config["confirmed_assumptions"],
            "component_meanings": {"peak_A": "product", "peak_B": "substrate"},
            "formula": "product_area / (product_area + substrate_area)",
            "sensitivity_fraction_range": decomposition["sensitivity_signal_fraction_ranges"]["peak_A"],
            "sensitivity_scope": decomposition["sensitivity_scope"], "uncertainty_note": decomposition["uncertainty_note"],
            "settings_count": decomposition["settings_count"],
            "settings_at_grid_boundary": decomposition["settings_at_grid_boundary"],
            "decomposition_result": "decomposition/analysis.json"}


def annotate_recipe(config, image_width):
    # Presentation adapter: place source-coordinate ROIs on this recipe's flip/crop derivative.
    from ij import IJ
    from ij.gui import Roi, OvalRoi, Overlay, TextRoi
    from java.awt import Color, Font
    ox, oy, cw, ch = config["crop_original_xywh"]
    flipped = config.get("flip_horizontal", False)
    fx = image_width - ox - cw if flipped else ox
    preview = IJ.openImage(os.path.join(config["output_dir"], "image", "gel_prepared_preview.png"))
    try:
        overlay = Overlay()
        font = Font("Arial", Font.PLAIN, 22)
        regions = [(lane["name"], [lane["x"][0], lane["peak_y"][0], lane["x"][1] - lane["x"][0],
                    lane["peak_y"][1] - lane["peak_y"][0]], False) for lane in config["lanes"]]
        regions.extend([(r["name"], r["original_xywh"], True) for r in config.get("excluded_regions", [])])
        for name, (x, y, w, h), excluded in regions:
            px = image_width - x - w - fx if flipped else x - fx
            roi = (OvalRoi if excluded else Roi)(px, y - oy, w, h)
            roi.setName(name)
            color = Color(150, 90, 0) if excluded else Color(0, 110, 210)
            roi.setStrokeColor(color)
            roi.setStrokeWidth(2)
            overlay.add(roi)
            label = TextRoi(px + 3, max(0, y - oy - 29), name, font)
            label.setStrokeColor(color)
            overlay.add(label)
        preview.setOverlay(overlay)
        IJ.saveAs(preview.flatten(), "PNG", os.path.join(config["output_dir"], "gel_annotated.png"))
    finally:
        preview.close()


def run(config):
    from ij import IJ
    output = config["output_dir"]
    if not os.path.isabs(output) or (os.path.exists(output) and (not os.path.isdir(output) or os.listdir(output))):
        raise ValueError("Choose a fresh or empty absolute output directory")
    source_path = globals().get("GEL_WORKFLOW_SOURCE_PATH", globals().get("__file__"))
    script_dir = globals().get("GEL_SCRIPT_DIR") or (os.path.dirname(source_path) if source_path else None)
    if not script_dir:
        raise ValueError("Set GEL_SCRIPT_DIR or GEL_WORKFLOW_SOURCE_PATH to locate the assay scripts")
    scripts = {}
    for name in ("adjust_and_export_image", "quantify_gel_peaks", "decompose_peaks"):
        path = os.path.join(script_dir, name + "_jython.py")
        scripts[name] = runpy.run_path(path, init_globals={"ASSAY_LIBRARY_ONLY": True})
    source = IJ.openImage(config["input_path"])
    if source is None:
        raise ValueError("ImageJ could not open input_path")
    try:
        width, height = source.getWidth(), source.getHeight()
        validate_config(config, width, height, source.getBitDepth())
        if source.getStackSize() != 1:
            raise ValueError("Select one image plane")
    finally:
        source.close()
    if not os.path.isdir(output):
        os.makedirs(output)
    image_config, quant_config, fit_config = build_assay_configs(config, width)
    image_result = scripts["adjust_and_export_image"]["run"](image_config)
    quant_result = scripts["quantify_gel_peaks"]["run"](quant_config)
    conversion = None
    if fit_config:
        with open(quant_result["profiles_path"], "r") as handle:
            profile_data = json.load(handle)
        peak_config, peak_data = build_peak_decomposition(config, fit_config, profile_data)
        save_json(peak_config["curves_path"], peak_data)
        decomposition = scripts["decompose_peaks"]["run"](peak_config)
        conversion = interpret_conversion(decomposition, fit_config)
        conversion_dir = os.path.join(output, "conversion")
        os.makedirs(conversion_dir)
        save_json(os.path.join(conversion_dir, "conversion_summary.json"), conversion)
        save_json(os.path.join(conversion_dir, "analysis.json"), conversion)
    if image_result["source_sha256"] != quant_result["source_sha256"]:
        raise RuntimeError("Image source changed between assays")
    annotate_recipe(config, width)
    save_json(os.path.join(output, "run_config.json"), config)
    workflow_hash = None
    if source_path and os.path.isfile(source_path):
        workflow_hash = sha256(source_path)
        with open(source_path, "rb") as handle:
            code = handle.read()
        with open(os.path.join(output, "workflow_jython.py"), "wb") as handle:
            handle.write(code)
    summary = {"workflow_version": "3.0", "workflow_source_sha256": workflow_hash,
               "source_sha256": quant_result["source_sha256"], "source_hash_unchanged": True,
               "original_size_px": [width, height], "areas": quant_result["areas"], "conversion": conversion,
               "assays": {"image": "image/analysis.json", "quantification": "quantification/analysis.json",
                          "conversion": "conversion/analysis.json" if conversion else None},
               "coordinate_note": "Quantification uses source pixels independently of presentation transforms"}
    save_json(os.path.join(output, "analysis.json"), summary)
    manifest = {}
    for directory, _, files in os.walk(output):
        for name in files:
            path = os.path.join(directory, name)
            manifest[os.path.relpath(path, output)] = sha256(path)
    save_json(os.path.join(output, "output_manifest.json"), manifest)
    return {"output_dir": output, "areas": summary["areas"], "conversion": conversion["primary"] if conversion else None}


if not globals().get("GEL_WORKFLOW_LIBRARY_ONLY", False):
    config = globals().get("GEL_CONFIG")
    if config is None:
        path = globals().get("GEL_CONFIG_PATH")
        if path is None:
            from ij import IJ
            path = IJ.getFilePath("Select gel workflow JSON configuration")
        if path is None:
            raise ValueError("Select a gel workflow configuration")
        with open(str(path), "r") as handle:
            config = json.load(handle)
    status = json.dumps(run(config), allow_nan=False)
