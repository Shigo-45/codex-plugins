#!/usr/bin/env python3
"""Build a relocatable plugin using explicit source allowlists."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"


def build(output: Path, *, rebuild_web: bool = True) -> Path:
    checkout = ROOT / "gemini-web-bridge/plugins/gemini-web-bridge"
    web = checkout if checkout.is_dir() else ROOT / "vendor/gemini-web"
    existing = ROOT / "plugins/gemini-baton"
    if rebuild_web and checkout.is_dir():
        subprocess.run(["npm", "run", "build"], cwd=web, check=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="write-to-me-build-") as temp:
        stage = Path(temp) / "gemini-baton"
        shutil.copytree(ROOT / "packaging/gemini-baton", stage, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        api = ROOT / "gemini-baton"
        for name in ("pyproject.toml", "uv.lock"):
            target = stage / "runtime" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(api / name, target)
        shutil.copytree(api / "src/gemini_baton", stage / "runtime/src/gemini_baton", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Build metadata uses a portable README, without developer-local paths.
        shutil.copyfile(stage / "README.md", stage / "runtime/README.md")
        shutil.copytree(ROOT / "skills/write-to-me", stage / "skills/write-to-me", symlinks=True)
        skill = stage / "skills/write-to-me/SKILL.md"
        text = skill.read_text()
        start = text.index("```bash")
        end = text.index("```", start + 7) + 3
        text = text[:start] + "```bash\npython3 <plugin-root>/scripts/run.py write\n# stdin JSON: {\"prompt\":\"任务要求与精选上下文\",\"cwd\":\"/absolute/workspace\",\"request_id\":\"task-unique-id\"}\npython3 <plugin-root>/scripts/run.py write-result JOB_ID --wait-seconds 30\n```" + text[end:]
        text = text.replace("- **默认路线**：", "- **用户配置**：先读取`gemini_status.writing`确认生效路线；通过`gemini_settings`打开插件本地设置页，调整启用顺序并完成所需账号登录。设置影响新任务，已受理任务沿用创建时快照。\n- **默认路线**：")
        text = text.replace("先核验`gemini_status.writing.routes`与当前默认顺序一致", "先核验`gemini_status.writing.routes`与用户设置的顺序一致")
        skill.write_text(text)
        (stage / "web").mkdir()
        for name in ("gemini-web-cli.mjs", "gemini-web-cli.mjs.LEGAL.txt", "THIRD_PARTY_LICENSES.txt"):
            source = (web / "dist" / name) if checkout.is_dir() else existing / "web" / name
            if source.exists():
                shutil.copyfile(source, stage / "web" / name)
        shutil.copyfile((ROOT / "gemini-web-bridge/LICENSE") if checkout.is_dir() else web / "LICENSE", stage / "web/LICENSE")
        previous = json.loads((existing / "BUILD.json").read_text()) if not checkout.is_dir() else {}
        provenance = {
            "version": VERSION,
            "web_upstream": subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=web, text=True).strip() if checkout.is_dir() else previous["web_upstream"],
            "web_base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=web, text=True).strip() if checkout.is_dir() else previous["web_base_commit"],
            "web_includes_local_changes": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=web)) if checkout.is_dir() else previous["web_includes_local_changes"],
            "sha256": {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(stage.rglob("*")) if p.is_file()},
        }
        (stage / "BUILD.json").write_text(json.dumps(provenance, indent=2) + "\n")
        forbidden = (str(Path.home()) + "/",)
        for path in stage.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"symlink in release: {path}")
            if path.is_file() and path.suffix in {".md", ".json", ".toml", ".yaml"}:
                content = path.read_text()
                if any(item in content for item in forbidden):
                    raise ValueError(f"developer-local path in release: {path.relative_to(stage)}")
        installed = ROOT / "plugins/gemini-baton"
        if installed.exists():
            shutil.rmtree(installed)
        shutil.copytree(stage, installed)
        archive = output / f"gemini-baton-{VERSION}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    bundle.write(path, str(path.relative_to(stage.parent)))
        (archive.with_suffix(".zip.sha256")).write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
        return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-web-build", action="store_true")
    args = parser.parse_args()
    print(build(args.output.resolve(), rebuild_web=not args.skip_web_build))
