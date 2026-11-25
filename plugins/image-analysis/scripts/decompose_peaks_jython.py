#@output String status
"""Assay: nonnegative decomposition of uniformly sampled 1-D curves using reference peaks.

Set ASSAY_CONFIG or ASSAY_CONFIG_PATH. Components have arbitrary names and may
share a reference or use different measured reference shapes. Shift grids and
explicit width sets define the search; each reference is normalized to unit area.
Inputs are signed, already baseline-corrected signals in a common x/signal unit.
Areas and fractions describe signal, with domain interpretation supplied by the
calling workflow. Requires only Jython standard library and Fiji for PNG plots.
"""
import csv
import hashlib
import itertools
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
        def encoded(value):
            return value.encode("utf-8") if sys.version_info[0] == 2 and isinstance(value, type(u"")) else value
        writer = csv.DictWriter(handle, [encoded(field) for field in fields])
        writer.writeheader()
        writer.writerows({encoded(key): encoded(value) for key, value in record.items()} for record in records)
    finally:
        handle.close()

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

def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def finite(value):
    return not isinstance(value, bool) and isinstance(value, numbers.Real) and not math.isnan(value) and not math.isinf(value)


def interpolate(values, position, x0, spacing):
    index = (position - x0) / spacing
    left = int(math.floor(index))
    fraction = index - left
    a = values[left] if 0 <= left < len(values) else 0.0
    b = values[left + 1] if 0 <= left + 1 < len(values) else 0.0
    return a * (1 - fraction) + b * fraction


def component(template, x, x0, spacing, center, shift, scale):
    return [interpolate(template, (value - center - shift) / scale + center, x0, spacing) / scale for value in x]


def solve_linear(matrix, rhs):
    count = len(rhs)
    work = [list(row) + [value] for row, value in zip(matrix, rhs)]
    threshold = max(abs(value) for row in matrix for value in row) * 1e-12
    for column in range(count):
        pivot = max(range(column, count), key=lambda i: abs(work[i][column]))
        if abs(work[pivot][column]) <= threshold:
            return None
        work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        work[column] = [value / divisor for value in work[column]]
        for row in range(count):
            if row != column:
                factor = work[row][column]
                work[row] = [a - factor * b for a, b in zip(work[row], work[column])]
    return [row[-1] for row in work]


def nnls_gram(gram, rhs):
    """Small dense NNLS; closed form for one/two columns, active set for more."""
    count = len(rhs)
    if count == 1:
        return [max(0.0, rhs[0] / gram[0][0])]
    if count == 2:
        aa, ab, bb = gram[0][0], gram[0][1], gram[1][1]
        candidates = [[max(0.0, rhs[0] / aa), 0.0], [0.0, max(0.0, rhs[1] / bb)]]
        determinant = aa * bb - ab * ab
        if determinant > 1e-12 * aa * bb:
            a = (rhs[0] * bb - rhs[1] * ab) / determinant
            b = (rhs[1] * aa - rhs[0] * ab) / determinant
            if a >= 0 and b >= 0:
                candidates.append([a, b])
        return min(candidates, key=lambda a: -2 * dot(a, rhs) + sum(a[i] * dot(gram[i], a) for i in range(count)))
    amplitudes = [0.0] * count
    passive, blocked = set(), set()
    gradient_tolerance = max(max(abs(v) for v in rhs), 1e-300) * 1e-11
    iterations = 0
    while True:
        gradient = [rhs[i] - dot(gram[i], amplitudes) for i in range(count)]
        eligible = [i for i in range(count) if i not in passive and i not in blocked and gradient[i] > gradient_tolerance]
        if not eligible:
            return amplitudes
        entering = max(eligible, key=lambda i: gradient[i])
        passive.add(entering)
        while True:
            iterations += 1
            if iterations > max(100, 50 * count):
                raise ValueError("NNLS did not converge; inspect reference conditioning")
            indices = sorted(passive)
            solution = solve_linear([[gram[i][j] for j in indices] for i in indices], [rhs[i] for i in indices])
            if solution is None:
                passive.remove(entering)
                blocked.add(entering)
                break
            candidate = [0.0] * count
            for i, value in zip(indices, solution):
                candidate[i] = value
            if all(candidate[i] > 0 for i in indices):
                amplitudes = candidate
                break
            ratios = [amplitudes[i] / (amplitudes[i] - candidate[i]) for i in indices
                      if candidate[i] <= 0 and amplitudes[i] > candidate[i]]
            alpha = min(ratios) if ratios else 0.0
            amplitudes = [max(0.0, a + alpha * (b - a)) for a, b in zip(amplitudes, candidate)]
            tolerance = max(max(amplitudes), 1e-300) * 1e-12
            passive = {i for i in passive if amplitudes[i] > tolerance}
            if not passive:
                blocked.add(entering)
                break


def make_template(reference, x, definition, spacing):
    lo, hi = definition["support"]
    values = [value if lo <= position < hi else 0.0 for position, value in zip(x, reference)]
    if definition.get("negative_reference_policy", "reject") == "clip":
        values = [max(0.0, value) for value in values]
    elif any(value < 0 for value in values):
        raise ValueError("Reference contains negative values inside support; select a reference-noise policy")
    area = sum(values) * spacing
    if area <= 0:
        raise ValueError("Reference has no positive area inside support")
    return [value / area for value in values]


def fit_components(x, observed, templates, definitions, fit_range, width_sets):
    spacing = x[1] - x[0]
    indices = [i for i, value in enumerate(x) if fit_range[0] <= value < fit_range[1]]
    xx = [x[i] for i in indices]
    z = [observed[i] for i in indices]
    names = [d["name"] for d in definitions]
    zz = dot(z, z)
    best = None
    # Cache transformed shapes and their data dot-products across width sets.
    cache = {}
    for widths in width_sets:
        options = []
        for definition, template in zip(definitions, templates):
            name, scale = definition["name"], widths[definition["name"]]
            column_options = []
            for shift in definition["shifts"]:
                key = (name, scale, shift)
                if key not in cache:
                    curve = component(template, xx, x[0], spacing, definition["center"], shift, scale)
                    cache[key] = (shift, curve, dot(curve, curve), dot(curve, z))
                if cache[key][2] > 0:
                    column_options.append(cache[key])
            options.append(column_options)
        for columns in itertools.product(*options):
            count = len(columns)
            gram = [[columns[i][2] if i == j else dot(columns[i][1], columns[j][1])
                     for j in range(count)] for i in range(count)]
            rhs = [col[3] for col in columns]
            amplitudes = nnls_gram(gram, rhs)
            sse = max(0.0, zz - 2 * dot(amplitudes, rhs) + sum(amplitudes[i] * dot(gram[i], amplitudes) for i in range(count)))
            if best is None or sse < best["SSE"]:
                best = {"SSE": sse, "amplitudes": amplitudes, "columns": columns, "widths": widths, "gram": gram}
    if best is None or sum(best["amplitudes"]) <= 0:
        raise ValueError("Fit has no positive signal; inspect references and fitting range")
    total = sum(best["amplitudes"])
    curves = [[amplitude * v for v in column[1]] for amplitude, column in zip(best["amplitudes"], best["columns"])]
    fitted = [sum(curve[i] for curve in curves) for i in range(len(z))]
    residual = [a - b for a, b in zip(z, fitted)]
    sse = dot(residual, residual)
    mean = sum(z) / float(len(z))
    sst = sum((value - mean) ** 2 for value in z)
    components, boundary = [], []
    for i, (definition, amplitude, column) in enumerate(zip(definitions, best["amplitudes"], best["columns"])):
        name = definition["name"]
        scales = [widths[name] for widths in width_sets]
        if len(definition["shifts"]) > 1 and column[0] in (min(definition["shifts"]), max(definition["shifts"])):
            boundary.append(name + ":shift")
        if len(set(scales)) > 1 and best["widths"][name] in (min(scales), max(scales)):
            boundary.append(name + ":width")
        coverage = sum(column[1]) * spacing
        components.append({"name": name, "area": amplitude, "signal_fraction": amplitude / total,
                           "shift": column[0], "width_scale": best["widths"][name],
                           "template_fraction_in_fit": coverage, "in_fit_area": amplitude * coverage})
    cosines = []
    for i in range(len(names)):
        for j in range(i):
            similarity = best["gram"][i][j] / math.sqrt(best["gram"][i][i] * best["gram"][j][j])
            cosines.append({"components": [names[j], names[i]], "cosine_similarity": similarity})
    rank_deficient = solve_linear(best["gram"], [1.0] * len(names)) is None
    warnings = ["highly_similar_components:" + ",".join(v["components"]) for v in cosines if v["cosine_similarity"] >= 0.995]
    if rank_deficient:
        warnings.append("rank_deficient_reference_set")
    result = {"components": components, "SSE": sse, "RMSE": math.sqrt(sse / len(z)),
              "R_squared": 1 - sse / sst if sst > 0 else None,
              "observed_area_in_fit": sum(z) * spacing, "residual_area": sum(residual) * spacing,
              "grid_boundary_parameters": boundary, "component_similarities": cosines,
              "reference_set_full_rank_at_relative_tolerance_1e_12": not rank_deficient,
              "warnings": warnings,
              "zero_amplitude_components": [v["name"] for v in components if v["area"] == 0]}
    samples = [{"x": position, "observed": value, "fitted_sum": fitted[i], "residual": residual[i]}
               for i, (position, value) in enumerate(zip(xx, z))]
    for i, record in enumerate(samples):
        for name, curve in zip(names, curves):
            record["component:" + name] = curve[i]
    return result, samples


def validate_config(config, data):
    if data.get("schema") != "image-analysis.peak-curves.v1":
        raise ValueError("Expected peak-curves.v1 data with uniform x, observed curves and references")
    x = data["x"]
    if len(x) < 2 or any(not finite(v) for v in x):
        raise ValueError("Use at least two finite x samples")
    spacing = x[1] - x[0]
    if spacing <= 0 or any(abs((b - a) / spacing - 1) > 1e-7 for a, b in zip(x, x[1:])):
        raise ValueError("Use strictly increasing, uniformly sampled x coordinates")
    fit_range = config["fit_range"]
    if (len(fit_range) != 2 or any(not finite(v) for v in fit_range) or not x[0] <= fit_range[0] < fit_range[1] <= x[-1] + spacing
            or sum(fit_range[0] <= v < fit_range[1] for v in x) < 2):
        raise ValueError("Fit range must select at least two samples within the input")
    definitions = config["components"]
    names = [d["name"] for d in definitions]
    if not names or len(set(names)) != len(names) or any(not isinstance(name, type(u"")) and not isinstance(name, str) or not name for name in names):
        raise ValueError("Provide uniquely named components")
    for definition in definitions:
        if not definition["shifts"] or len(set(definition["shifts"])) != len(definition["shifts"]) or any(not finite(v) for v in definition["shifts"]):
            raise ValueError("Component shift grids require unique finite values")
        lo, hi = definition["support"]
        if not finite(lo) or not finite(hi) or not x[0] <= lo < hi <= x[-1] + spacing or not finite(definition["center"]):
            raise ValueError("Reference support and center must be finite and support must lie within the input")
        if definition.get("negative_reference_policy", "reject") not in ("reject", "clip"):
            raise ValueError("Select reject or clip for negative reference noise")
    models = config["models"]
    if not models or len(set(m["name"] for m in models)) != len(models):
        raise ValueError("Provide uniquely named search models")
    candidates = 0
    for model in models:
        if not model["width_sets"]:
            raise ValueError("Each model needs explicit width sets")
        for widths in model["width_sets"]:
            if set(widths) != set(names) or any(not finite(v) or v <= 0 for v in widths.values()):
                raise ValueError("Width sets must specify a positive finite scale for every component")
        count = len(model["width_sets"])
        for d in definitions:
            count *= len(d["shifts"])
        candidates += count
    datasets = data["datasets"]
    if not datasets or len(set(d["name"] for d in datasets)) != len(datasets):
        raise ValueError("Provide uniquely named datasets")
    if candidates * len(datasets) > config.get("max_grid_candidates", 250000):
        raise ValueError("Configured grid exceeds max_grid_candidates; narrow the search or increase the explicit limit")
    for dataset in datasets:
        curves = [dataset["observed"]] + [dataset["references"][d["reference"]] for d in definitions]
        if any(len(curve) != len(x) or any(not finite(v) for v in curve) for curve in curves):
            raise ValueError("All curves must be finite and match x samples and units")
    primary = config["primary"]
    if primary["dataset"] not in [v["name"] for v in datasets] or primary["model"] not in [v["name"] for v in models]:
        raise ValueError("Primary fit must identify a dataset and model")
    divisor = config.get("plot", {}).get("signal_divisor", 1.0)
    if not finite(divisor) or divisor <= 0:
        raise ValueError("plot.signal_divisor must be positive and finite")
    return spacing


def save_plot(path, x, curves, xlabel, ylabel, divisor):
    from ij import IJ
    from ij.gui import Plot
    from java.awt import Color, Font
    from jarray import array
    plot = Plot("", xlabel, ylabel)
    plot.setFont(Font("Arial", Font.PLAIN, 18))
    plot.setBackgroundColor(Color.white)
    plot.setFrameSize(850, 420)
    colors = [Color(35, 35, 35), Color(0, 120, 180), Color(220, 105, 0), Color(170, 40, 140), Color(0, 135, 95), Color(110, 90, 190)]
    for i, (label, values) in enumerate(curves):
        plot.setColor(colors[i % len(colors)])
        plot.setLineWidth(2)
        plot.addPoints(array([float(v) for v in x], "d"), array([v / divisor for v in values], "d"), Plot.LINE)
    plot.setColor(Color.black)
    plot.setLegend("\n".join(label for label, _ in curves), Plot.AUTO_POSITION)
    IJ.saveAs(plot.getImagePlus(), "PNG", path)


def run(config):
    adapted = dict(config)
    adapted["input_path"] = config["curves_path"]
    before = start_run(adapted)
    with open(config["curves_path"], "r") as handle:
        data = json.load(handle)
    spacing = validate_config(config, data)
    output = config["output_dir"]
    if not os.path.isdir(output):
        os.makedirs(output)
    results = []
    for dataset in data["datasets"]:
        templates = [make_template(dataset["references"][d["reference"]], data["x"], d, spacing) for d in config["components"]]
        for model in config["models"]:
            result, samples = fit_components(data["x"], dataset["observed"], templates, config["components"], config["fit_range"], model["width_sets"])
            result.update({"dataset": dataset["name"], "model": model["name"], "metadata": dataset.get("metadata", {})})
            results.append(result)
            if result["dataset"] == config["primary"]["dataset"] and result["model"] == config["primary"]["model"]:
                primary, primary_samples = result, samples
    names = [d["name"] for d in config["components"]]
    sensitivity = {name: [min(next(c["signal_fraction"] for c in r["components"] if c["name"] == name) for r in results),
                          max(next(c["signal_fraction"] for c in r["components"] if c["name"] == name) for r in results)] for name in names}
    summary = {"assay": "decompose_peaks", "version": "1.0", "source_sha256": before,
               "upstream_source_sha256": data.get("source_sha256"), "primary": primary,
               "x_unit": data.get("x_unit", "input x units"), "signal_unit": data.get("signal_unit", "input signal units"),
               "area_unit": data.get("signal_unit", "signal") + " * " + data.get("x_unit", "x"),
               "area_method": "Rectangular sum times uniform x spacing; unit-area empirical reference templates",
               "signal_fraction_definition": "Component area divided by sum of fitted component areas",
               "interpretation": "Signal fractions; response factors and domain meaning are supplied by the caller",
               "sensitivity_signal_fraction_ranges": sensitivity, "settings_count": len(results),
               "sensitivity_scope": "Configured datasets, reference templates and search models",
               "uncertainty_note": "Model sensitivity is distinct from replicate-based confidence intervals",
               "settings_at_grid_boundary": sum(bool(r["grid_boundary_parameters"]) for r in results)}
    save_json(os.path.join(output, "decomposition_summary.json"), summary)
    save_json(os.path.join(output, "all_fits.json"), results)
    records = []
    for result in results:
        for comp in result["components"]:
            record = dict(comp)
            record.update({key: result[key] for key in ("dataset", "model", "SSE", "RMSE", "R_squared")})
            record["grid_boundary_parameters"] = ";".join(result["grid_boundary_parameters"])
            records.append(record)
    save_csv(os.path.join(output, "model_sensitivity.csv"), sorted(records[0]), records)
    fields = ["x", "observed", "fitted_sum", "residual"] + ["component:" + name for name in names]
    save_csv(os.path.join(output, "fitted_components.csv"), fields, primary_samples)
    plot_config = config.get("plot", {})
    divisor = plot_config.get("signal_divisor", 1.0)
    if not finite(divisor) or divisor <= 0:
        raise ValueError("plot.signal_divisor must be positive and finite")
    xlabel = plot_config.get("x_label", "Position (" + summary["x_unit"] + ")")
    ylabel = plot_config.get("signal_label", "Signal (" + summary["signal_unit"] + ")" + (" / %g" % divisor if divisor != 1 else ""))
    xx = [r["x"] for r in primary_samples]
    if plot_config.get("enabled", True):
        save_plot(os.path.join(output, "peak_fit.png"), xx,
                  [(key, [r[key] for r in primary_samples]) for key in ["observed"] + ["component:" + n for n in names] + ["fitted_sum"]], xlabel, ylabel, divisor)
        save_plot(os.path.join(output, "fit_residual.png"), xx,
                  [("Residual", [r["residual"] for r in primary_samples])], xlabel, ylabel, divisor)
    return finish_run(adapted, summary)


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
