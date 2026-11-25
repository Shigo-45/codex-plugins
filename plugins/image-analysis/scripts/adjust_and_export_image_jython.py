#@output String status
"""Assay: configure image geometry and calibration, then export named derivatives.

Set ASSAY_CONFIG or ASSAY_CONFIG_PATH. Operations execute in list order; crop
coordinates refer to the current intermediate image. Supported operations:
flip, rotate (degrees clockwise; arbitrary angles use a fixed canvas), crop,
resize (resampling), set_calibration (physical pixel spacing). Native exports
preserve image bit depth; display exports explicitly map contrast to 8 bits.
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
    if bit_depth not in (8, 16, 24, 32):
        raise ValueError("Use a supported ImageJ image type")
    interpolating = False
    for operation in config.get("operations", []):
        kind = operation["op"]
        if kind == "flip":
            if operation["axis"] not in ("horizontal", "vertical"):
                raise ValueError("Flip axis must be horizontal or vertical")
        elif kind == "crop":
            x, y, w, h = operation["xywh"]
            interval([x, x + w], width, "crop x")
            interval([y, y + h], height, "crop y")
            width, height = w, h
        elif kind == "rotate":
            angle = operation["angle_degrees"]
            if math.isnan(angle) or math.isinf(angle):
                raise ValueError("Rotation angle must be finite")
            if angle % 90 == 0:
                if int(angle // 90) % 2:
                    width, height = height, width
            else:
                # Arbitrary rotations use the current fixed-size canvas.
                interpolating = True
        elif kind == "resize":
            if "factor" in operation:
                factor = operation["factor"]
                if not factor > 0 or math.isnan(factor) or math.isinf(factor):
                    raise ValueError("Resize factor must be positive and finite")
                width, height = max(1, int(round(width * factor))), max(1, int(round(height * factor)))
            else:
                width, height = operation["size_px"]
                interval([0, width], width, "resize width")
                interval([0, height], height, "resize height")
            interpolating = True
        elif kind == "set_calibration":
            for key in ("pixel_width", "pixel_height"):
                value = operation[key]
                if not value > 0 or math.isnan(value) or math.isinf(value):
                    raise ValueError("Calibration pixel sizes must be positive and finite")
            if not operation.get("unit"):
                raise ValueError("Calibration requires a unit")
        else:
            raise ValueError("Unknown image operation: " + kind)
        if operation.get("interpolation", "bilinear") not in ("nearest", "bilinear", "bicubic"):
            raise ValueError("Unknown interpolation method")
    exports = config["exports"]
    if not exports or len(set(v["filename"] for v in exports)) != len(exports):
        raise ValueError("Select uniquely named exports")
    for export in exports:
        name = export["filename"]
        if not name or os.path.basename(name) != name or name in (".", ".."):
            raise ValueError("Export filename must be a plain file name")
        if export["format"] not in ("tiff", "png", "jpeg") or export.get("mode", "native") not in ("native", "display"):
            raise ValueError("Choose tiff/png/jpeg and native/display mode")
        if export.get("mode", "native") == "display":
            bounds = export["display_range"]
            if len(bounds) != 2 or not bounds[0] < bounds[1] or any(math.isnan(v) or math.isinf(v) for v in bounds):
                raise ValueError("Display exports require a finite increasing intensity range")
            if bit_depth == 24:
                raise ValueError("Use native mode for RGB export")
        if export.get("mode", "native") == "native":
            if export["format"] == "jpeg" and bit_depth not in (8, 24):
                raise ValueError("Use display mode for JPEG conversion of high-bit-depth grayscale")
            if export["format"] == "png" and bit_depth == 32:
                raise ValueError("Use TIFF for native float pixels or display mode for PNG")
    return width, height, interpolating


def run(config):
    from ij import IJ, ImagePlus
    from ij.io import FileSaver
    from ij.process import ImageProcessor, LUT
    from java.awt import Color
    before = start_run(config)
    source = IJ.openImage(config["input_path"])
    if source is None:
        raise ValueError("ImageJ could not open input_path")
    try:
        if source.getStackSize() != 1:
            raise ValueError("Select one image plane")
        expected_w, expected_h, interpolating = validate_config(config, source.getWidth(), source.getHeight(), source.getBitDepth())
        processor = source.getProcessor().duplicate()
        processor.resetRoi()
        calibration = source.getCalibration().copy()
        interpolation = {"nearest": ImageProcessor.NONE, "bilinear": ImageProcessor.BILINEAR, "bicubic": ImageProcessor.BICUBIC}
        for operation in config.get("operations", []):
            kind = operation["op"]
            processor.setInterpolationMethod(interpolation[operation.get("interpolation", "bilinear")])
            if kind == "flip":
                if operation["axis"] == "horizontal":
                    processor.flipHorizontal()
                    calibration.xOrigin = processor.getWidth() - 1 - calibration.xOrigin
                else:
                    processor.flipVertical()
                    calibration.yOrigin = processor.getHeight() - 1 - calibration.yOrigin
            elif kind == "crop":
                x, y, w, h = operation["xywh"]
                processor.setRoi(x, y, w, h)
                processor = processor.crop()
                calibration.xOrigin -= x
                calibration.yOrigin -= y
            elif kind == "rotate":
                angle = operation["angle_degrees"]
                if angle % 90 == 0:
                    for _ in range(int(angle // 90) % 4):
                        old_height = processor.getHeight()
                        processor = processor.rotateRight()
                        calibration.pixelWidth, calibration.pixelHeight = calibration.pixelHeight, calibration.pixelWidth
                        calibration.xOrigin, calibration.yOrigin = old_height - 1 - calibration.yOrigin, calibration.xOrigin
                else:
                    if abs(calibration.pixelWidth / calibration.pixelHeight - 1) > 1e-9:
                        raise ValueError("Arbitrary rotation requires square pixels; resample or set calibration first")
                    processor.setBackgroundValue(operation.get("background_value", 0))
                    processor.rotate(angle)
                    radians = math.radians(angle)
                    cx, cy = (processor.getWidth() - 1) / 2.0, (processor.getHeight() - 1) / 2.0
                    dx, dy = calibration.xOrigin - cx, calibration.yOrigin - cy
                    calibration.xOrigin = cx + math.cos(radians) * dx - math.sin(radians) * dy
                    calibration.yOrigin = cy + math.sin(radians) * dx + math.cos(radians) * dy
            elif kind == "resize":
                ow, oh = processor.getWidth(), processor.getHeight()
                if "factor" in operation:
                    nw, nh = max(1, int(round(ow * operation["factor"]))), max(1, int(round(oh * operation["factor"])))
                else:
                    nw, nh = operation["size_px"]
                processor = processor.resize(nw, nh)
                calibration.pixelWidth *= float(ow) / nw
                calibration.pixelHeight *= float(oh) / nh
                calibration.xOrigin *= float(nw) / ow
                calibration.yOrigin *= float(nh) / oh
            elif kind == "set_calibration":
                calibration.pixelWidth, calibration.pixelHeight = operation["pixel_width"], operation["pixel_height"]
                calibration.setUnit(operation["unit"])
        image = ImagePlus("Adjusted image", processor)
        image.setCalibration(calibration)
        if (image.getWidth(), image.getHeight()) != (expected_w, expected_h):
            raise RuntimeError("Transformed dimensions differ from the configured operations")
        output = config["output_dir"]
        if not os.path.isdir(output):
            os.makedirs(output)
        records = []
        for export in config["exports"]:
            result = image
            if export.get("mode", "native") == "display":
                pp = processor.duplicate()
                pp.setMinAndMax(*export["display_range"])
                pp = pp.convertToByte(True)
                pp.setColorModel(LUT.createLutFromColor(Color.white))
                if export.get("dark_signal", True):
                    pp.invertLut()
                result = ImagePlus("Image display derivative", pp)
                result.setCalibration(calibration.copy())
            saver = FileSaver(result)
            path = os.path.join(output, export["filename"])
            saved = {"tiff": saver.saveAsTiff, "png": saver.saveAsPng, "jpeg": saver.saveAsJpeg}[export["format"]](path)
            if not saved or not os.path.isfile(path):
                raise IOError("ImageJ export failed: " + path)
            records.append({"path": path, "format": export["format"], "mode": export.get("mode", "native"),
                            "bit_depth": result.getBitDepth(), "lossy_format": export["format"] == "jpeg"})
        return finish_run(config, {"assay": "adjust_and_export_image", "version": "1.0", "source_sha256": before,
                   "original_size_px": [source.getWidth(), source.getHeight()],
                   "output_size_px": [image.getWidth(), image.getHeight()], "native_bit_depth": image.getBitDepth(),
                   "resampling_requested": interpolating, "fiji_version": IJ.getVersion(),
                   "operation_coordinate_frame": "Each operation uses the image produced by the preceding operation",
                   "arbitrary_rotation_canvas": "Fixed size; corners may leave the canvas",
                   "calibration_note": "Pixel spacing follows axis swaps and resizing; operation list records orientation",
                   "pixel_width": calibration.pixelWidth, "pixel_height": calibration.pixelHeight,
                   "calibration_unit": calibration.getUnit(), "exports": records})
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
