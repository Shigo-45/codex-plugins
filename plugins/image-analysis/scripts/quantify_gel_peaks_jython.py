#@output String status
"""Assay: rectangular gel-lane densitometry, including profiles, baseline and integration.

Set ASSAY_CONFIG or ASSAY_CONFIG_PATH. Optional profile-only lanes feed a separate
reference-mixture assay. All bounds use this input image, zero-based/end-exclusive.
Uses Fiji standard APIs and the Jython standard library.
"""
import csv
import hashlib
import json
import math
import numbers
import os
import sys

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


def median(values):
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        raise ValueError("Background window must contain pixels")
    return (ordered[(count - 1) // 2] + ordered[count // 2]) / 2.0


def correct_profile(raw, width, top, bottom, polarity):
    a = median([raw[y] for y in range(*top)])
    b = median([raw[y] for y in range(*bottom)])
    ya = (top[0] + top[1] - 1) / 2.0
    yb = (bottom[0] + bottom[1] - 1) / 2.0
    baseline = {y: a + (b - a) * (y - ya) / (yb - ya) for y in raw}
    net = {y: polarity * (raw[y] - baseline[y]) * width for y in raw}
    return net, baseline


def raw_profile(processor, x_bounds, rows):
    x0, x1 = x_bounds
    # Read stored unsigned pixels, independently of any saved density-calibration table.
    return {y: sum(processor.getPixel(x, y) for x in range(x0, x1)) / float(x1 - x0) for y in rows}


def save_plot(output_path, x, curves, ylabel):
    # Java imports are runtime-only so the numerical functions can be tested with CPython.
    from ij import IJ
    from ij.gui import Plot
    from java.awt import Color, Font
    from jarray import array
    plot = Plot("", "Original vertical position (px)", ylabel)
    plot.setFont(Font("Arial", Font.PLAIN, 18))
    plot.setBackgroundColor(Color.white)
    plot.setFrameSize(850, 420)
    colors = [Color(35, 35, 35), Color(0, 120, 180), Color(220, 105, 0), Color(170, 40, 140)]
    for i, (label, values) in enumerate(curves):
        plot.setColor(colors[i % len(colors)])
        plot.setLineWidth(2)
        plot.addPoints(array([float(v) for v in x], "d"), array([v / 1e6 for v in values], "d"), Plot.LINE)
    for i, (label, _) in enumerate(curves):
        plot.setColor(colors[i % len(colors)])
        plot.addLabel(0.54, 0.08 + i * 0.075, label)
    IJ.saveAs(plot.getImagePlus(), "PNG", output_path)


def start_run(config):
    source, output = config["input_path"], config["output_dir"]
    if not os.path.isabs(source) or not os.path.isabs(output):
        raise ValueError("Use absolute input_path and output_dir")
    if os.path.exists(output) and (not os.path.isdir(output) or os.listdir(output)):
        raise ValueError("Choose a fresh or empty output directory")
    return sha256(source)


def finish_run(config, summary):
    output = config["output_dir"]
    if sha256(config["input_path"]) != summary["source_sha256"]:
        raise RuntimeError("Input changed during analysis; inspect outputs")
    summary["source_hash_unchanged"] = True
    save_json(os.path.join(output, "run_config.json"), config)
    path = globals().get("ASSAY_SOURCE_PATH", globals().get("__file__"))
    if path and os.path.isfile(path):
        summary["script_sha256"] = sha256(path)
        with open(path, "rb") as handle:
            code = handle.read()
        with open(os.path.join(output, "assay_jython.py"), "wb") as handle:
            handle.write(code)
    save_json(os.path.join(output, "analysis.json"), summary)
    save_json(os.path.join(output, "output_manifest.json"),
              {name: sha256(os.path.join(output, name)) for name in sorted(os.listdir(output))
               if os.path.isfile(os.path.join(output, name))})
    return summary


def validate_config(config, width, height, bit_depth):
    if bit_depth not in (8, 16):
        raise ValueError("Use a single-plane unsigned 8-bit or 16-bit quantitative source")
    if config.get("signal_polarity") not in ("high", "low"):
        raise ValueError("Set signal_polarity from raw values: high or low")
    top, bottom = config["background_y"]["top"], config["background_y"]["bottom"]
    interval(top, height, "top background")
    interval(bottom, height, "bottom background")
    if top[1] > bottom[0]:
        raise ValueError("Background windows must be separate and ordered")
    shifts = config.get("background_shifts_px", [0])
    if (not shifts or len(set(shifts)) != len(shifts)
            or config.get("primary_background_shift_px", 0) not in shifts):
        raise ValueError("Provide unique shifts including the primary background setting")
    for shift in shifts:
        interval([top[0] + shift, top[1] + shift], height, "shifted top background")
        interval([bottom[0] + shift, bottom[1] + shift], height, "shifted bottom background")
    bounds = config.get("profile_y", [top[0] + min(shifts), bottom[1] + max(shifts)])
    interval(bounds, height, "profile y")
    if bounds[0] > top[0] + min(shifts) or bounds[1] < bottom[1] + max(shifts):
        raise ValueError("profile_y must include every background window")
    lanes = config["lanes"]
    if not lanes or len(set(v["name"] for v in lanes)) != len(lanes):
        raise ValueError("Select uniquely named lanes")
    measured = []
    for lane in lanes:
        interval(lane["x"], width, "lane x")
        peak = lane.get("peak_y")
        if peak is not None:
            interval(peak, height, "peak y")
            if not top[1] <= peak[0] < peak[1] <= bottom[0]:
                raise ValueError("Nominal backgrounds must flank the peak")
            measured.append(lane["name"])
    if not measured or config.get("relative_to_lane", measured[0]) not in measured:
        raise ValueError("Select a reference from the integrated lanes")
    display = config.get("display_range", [0, 2 ** bit_depth - 1])
    if len(display) != 2 or not 0 <= display[0] < display[1] <= 2 ** bit_depth - 1:
        raise ValueError("Invalid display_range")
    return bounds


def run(config):
    from ij import IJ, ImagePlus
    from ij.gui import Roi, OvalRoi, Overlay, TextRoi
    from ij.io import RoiEncoder
    from ij.process import LUT
    from java.awt import Color, Font
    from java.io import FileOutputStream
    from java.util.zip import ZipOutputStream, ZipEntry
    before = start_run(config)
    source = IJ.openImage(config["input_path"])
    if source is None:
        raise ValueError("ImageJ could not open input_path")
    try:
        if source.getStackSize() != 1 or source.getNChannels() != 1:
            raise ValueError("Select one plane and channel")
        width, height, depth = source.getWidth(), source.getHeight(), source.getBitDepth()
        bounds = validate_config(config, width, height, depth)
        output = config["output_dir"]
        if not os.path.isdir(output):
            os.makedirs(output)
        processor = source.getProcessor()
        rows = list(range(*bounds))
        shifts = config.get("background_shifts_px", [0])
        primary_shift = config.get("primary_background_shift_px", 0)
        top, bottom = config["background_y"]["top"], config["background_y"]["bottom"]
        polarity = 1 if config["signal_polarity"] == "high" else -1
        profiles, table, areas, sensitivity, rois, curves = [], [], [], [], [], []
        overlay = Overlay()
        font = Font("Arial", Font.PLAIN, 22)
        for lane in config["lanes"]:
            name = lane["name"]
            x0, x1 = lane["x"]
            raw = raw_profile(processor, lane["x"], rows)
            peak = lane.get("peak_y")
            for shift in shifts:
                net, baseline = correct_profile(raw, x1 - x0,
                    [top[0] + shift, top[1] + shift], [bottom[0] + shift, bottom[1] + shift], polarity)
                profiles.append({"lane": name, "background_shift_px": shift, "x_bounds": lane["x"],
                                 "y_start": bounds[0], "net_sum_AU_per_row": [net[y] for y in rows]})
                for y in rows:
                    table.append({"lane": name, "background_shift_px": shift, "original_y_px": y,
                                  "raw_mean_AU": raw[y], "baseline_mean_AU": baseline[y],
                                  "net_sum_AU_per_row": net[y],
                                  "inside_peak_roi": int(peak is not None and peak[0] <= y < peak[1])})
                if peak is not None:
                    y0, y1 = peak
                    raw_area = sum(raw[y] * (x1 - x0) for y in range(y0, y1))
                    bg_area = sum(baseline[y] * (x1 - x0) for y in range(y0, y1))
                    record = {"lane": name, "background_shift_px": shift,
                              "net_peak_area_AU_px": polarity * (raw_area - bg_area),
                              "raw_integral_AU_px": raw_area, "background_integral_AU_px": bg_area,
                              "peak_original_y_px": max(range(y0, y1), key=lambda y: net[y])}
                    sensitivity.append(record)
                    if shift == primary_shift:
                        values = [processor.getPixel(x, y) for y in range(y0, y1) for x in range(x0, x1)]
                        area = dict(record)
                        area.update({"max_raw_value": max(values), "min_raw_value": min(values),
                                     "pixels_at_high_rail": sum(v == 2 ** depth - 1 for v in values),
                                     "pixels_at_zero": sum(v == 0 for v in values)})
                        areas.append(area)
                        curves.append((name, [net[y] for y in rows]))
            if peak is not None:
                roi = Roi(x0, peak[0], x1 - x0, peak[1] - peak[0])
                roi.setName(name)
                roi.setStrokeColor(Color(0, 110, 210))
                roi.setStrokeWidth(2)
                rois.append(roi)
                overlay.add(roi)
                label = TextRoi(x0 + 3, max(0, peak[0] - 29), name, font)
                label.setStrokeColor(Color(0, 110, 210))
                overlay.add(label)
                for suffix, bg in (("bg_top", top), ("bg_bottom", bottom)):
                    roi = Roi(x0, bg[0], x1 - x0, bg[1] - bg[0])
                    roi.setName(name + "_" + suffix)
                    rois.append(roi)
        reference = config.get("relative_to_lane", areas[0]["lane"])
        ref_area = next(v["net_peak_area_AU_px"] for v in areas if v["lane"] == reference)
        for area in areas:
            area["relative_to_reference_percent"] = 100 * area["net_peak_area_AU_px"] / ref_area if ref_area > 0 else None
        save_csv(os.path.join(output, "peak_areas.csv"), sorted(areas[0]), areas)
        save_csv(os.path.join(output, "background_sensitivity.csv"), sorted(sensitivity[0]), sensitivity)
        save_csv(os.path.join(output, "lane_profiles.csv"), sorted(table[0]), table)
        profile_data = {"schema": "image-analysis.gel-profiles.v1", "source_sha256": before,
                        "coordinate_frame": "Input image, zero-based pixel rows; signed net raw intensity sum per row",
                        "background_y": config["background_y"], "signal_polarity": config["signal_polarity"],
                        "profiles": profiles}
        save_json(os.path.join(output, "profile_data.json"), profile_data)
        stream = ZipOutputStream(FileOutputStream(os.path.join(output, "gel_rois_original.zip")))
        try:
            for roi in rois:
                stream.putNextEntry(ZipEntry(roi.getName() + ".roi"))
                RoiEncoder(stream).write(roi)
                stream.closeEntry()
        finally:
            stream.close()
        pp = processor.duplicate()
        pp.setMinAndMax(*config.get("display_range", [0, 2 ** depth - 1]))
        pp = pp.convertToByte(True)
        pp.setColorModel(LUT.createLutFromColor(Color.white))
        if polarity == 1:
            pp.invertLut()
        preview = ImagePlus("Gel quantification ROIs", pp)
        preview.setOverlay(overlay)
        IJ.saveAs(preview.flatten(), "PNG", os.path.join(output, "gel_annotated_original.png"))
        save_plot(os.path.join(output, "lane_profiles.png"), rows, curves, "Net signal (10^6 AU / row)")
        return finish_run(config, {"assay": "quantify_gel_peaks", "version": "1.0", "source_sha256": before,
                   "original_size_px": [width, height], "bit_depth": depth, "fiji_version": IJ.getVersion(),
                   "coordinate_frame": "Input image; zero-based, end-exclusive ROI bounds",
                   "background_method": "Line through median row means in two background windows",
                   "signal_method": "Signed raw-pixel integration after baseline subtraction",
                   "warnings": [a["lane"] + ": digital intensity rail reached" for a in areas
                                if a["pixels_at_high_rail"] or a["pixels_at_zero"]],
                   "relative_reference_lane": reference, "areas": areas,
                   "profiles_path": os.path.join(output, "profile_data.json")})
    finally:
        source.close()


if not globals().get("ASSAY_LIBRARY_ONLY", False):
    config = globals().get("ASSAY_CONFIG")
    if config is None:
        path = globals().get("ASSAY_CONFIG_PATH")
        if path is None:
            from ij import IJ
            path = IJ.getFilePath("Select assay JSON configuration")
        if path is None:
            raise ValueError("Select an assay configuration")
        with open(str(path), "r") as handle:
            config = json.load(handle)
    result = run(config)
    status = json.dumps({"output_dir": config["output_dir"], "assay": result["assay"]}, allow_nan=False)
