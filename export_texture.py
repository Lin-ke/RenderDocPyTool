# -*- coding: utf-8 -*-
r"""Export textures listed in a search_texture pass report.

This script intentionally uses only ``tool.rdoc_interface`` as the RenderDoc access
layer. It exports each unique texture resource from the matching passes once,
and writes a manifest mapping passes back to the exported files.

Run with Python 3.6. Runtime options are read from ``rdc_tool.json``:

  C:\Users\weiyupeng\AppData\Local\Programs\Python\Python36\python.exe export_texture.py
"""

from __future__ import print_function

import json
import os
import re
import sys
import time
import traceback


CONFIG_FILENAME = "rdc_tool.json"

CaptureSession = None
file_type_from_name = None
initialise_replay = None
is_success = None
result_msg = None
shutdown_replay = None
texture_file_extension = None


def _add_candidate(candidates, path):
    if not path:
        return
    path = os.path.abspath(path)
    if path not in candidates:
        candidates.append(path)


def load_json_config(path=None):
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    candidates = []
    if path:
        _add_candidate(candidates, path)
    _add_candidate(candidates, os.path.join(os.getcwd(), CONFIG_FILENAME))
    _add_candidate(candidates, os.path.join(here, CONFIG_FILENAME))
    _add_candidate(candidates, os.path.join(project_root, CONFIG_FILENAME))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f), os.path.abspath(candidate)
    return {}, "<defaults>"


def required_value(value, name, cfg_path):
    if value is not None and value != "":
        return value
    raise RuntimeError("missing required config/API value '%s' (config: %s)" %
                       (name, cfg_path))


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def configure_renderdoc_path(cfg, cfg_path, renderdoc_dir=None, pymodules_dir=None):
    rd_cfg = cfg.get("renderdoc") or {}
    dev_dir = (renderdoc_dir or rd_cfg.get("development_dir") or
               rd_cfg.get("renderdoc_dir"))
    dev_dir = required_value(dev_dir, "renderdoc.development_dir", cfg_path)
    pymodules = pymodules_dir or rd_cfg.get("pymodules_dir") or os.path.join(dev_dir, "pymodules")
    path_parts = [dev_dir, pymodules]
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(path_parts + [old_path])
    old_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [pymodules] + ([old_pythonpath] if old_pythonpath else []))
    if pymodules not in sys.path:
        sys.path.insert(0, pymodules)
    return dev_dir, pymodules


def import_rdoc_interface():
    global CaptureSession, file_type_from_name, initialise_replay
    global is_success, result_msg, shutdown_replay, texture_file_extension
    from RenderDocPyTool.rdoc_interface import (CaptureSession as _CaptureSession,
                                     file_type_from_name as _file_type_from_name,
                                     initialise_replay as _initialise_replay,
                                     is_success as _is_success,
                                     result_msg as _result_msg,
                                     shutdown_replay as _shutdown_replay,
                                     texture_file_extension as _texture_file_extension)
    CaptureSession = _CaptureSession
    file_type_from_name = _file_type_from_name
    initialise_replay = _initialise_replay
    is_success = _is_success
    result_msg = _result_msg
    shutdown_replay = _shutdown_replay
    texture_file_extension = _texture_file_extension


def safe_name(value):
    value = str(value or "unnamed")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._") or "unnamed"


def resource_key(value):
    return str(value).replace("ResourceId::", "").strip()


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def parse_pass_report(path):
    capture = None
    fmt = None
    passes = []
    current = None
    eid_re = re.compile(r"^EID\s+(\d+)\s+count=(\d+)\s*(.*)$")
    tex_re = re.compile(
        r"^\s*-\s+(.*?)\s+\(ResourceId::([^\)]+)\)\s+(.+?)\s+(\d+)x(\d+)(?:\s+.*)?$")

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("Capture"):
                capture = line.split(":", 1)[1].strip()
                continue
            if line.startswith("Format"):
                fmt = line.split(":", 1)[1].strip()
                continue
            m = eid_re.match(line)
            if m:
                current = {
                    "eid": int(m.group(1)),
                    "count": int(m.group(2)),
                    "name": m.group(3).strip(),
                    "textures": [],
                }
                passes.append(current)
                continue
            m = tex_re.match(line)
            if m and current is not None:
                current["textures"].append({
                    "name": m.group(1).strip(),
                    "resource_id": m.group(2).strip(),
                    "format": m.group(3).strip(),
                    "width": int(m.group(4)),
                    "height": int(m.group(5)),
                })

    if not capture:
        raise RuntimeError("capture path not found in pass report: %s" % path)
    return capture, fmt or "", passes


def unique_textures(passes):
    out = {}
    first_order = []
    for pass_info in passes:
        for tex in pass_info["textures"]:
            rid = tex["resource_id"]
            if rid not in out:
                out[rid] = dict(tex)
                out[rid]["passes"] = []
                first_order.append(rid)
            out[rid]["passes"].append(pass_info["eid"])
    return [out[rid] for rid in first_order]


def enrich_from_capture(tool, tex):
    state = tool.getTexture("ResourceId::" + tex["resource_id"])
    if state is None:
        state = tool.getTexture(tex["resource_id"])
    if state is None:
        return None
    tex["capture_name"] = state.name
    tex["format"] = state.format or tex.get("format", "")
    tex["width"] = state.width or tex.get("width", 0)
    tex["height"] = state.height or tex.get("height", 0)
    tex["mips"] = state.mips
    tex["arraysize"] = state.arraysize
    return state


def verify_bindings(tool, passes, wanted_ids):
    seen = set()
    by_eid = {}
    for pass_info in passes:
        textures = tool.fast_pixel_textures(pass_info["eid"])
        ids = set(resource_key(t.handle) for t in textures)
        by_eid[pass_info["eid"]] = sorted(ids & wanted_ids)
        seen.update(ids & wanted_ids)
    return seen, by_eid


def export_one(tool, state, tex, out_dir, ext, file_type, mip, slice_index):
    base = "tex_%s_%dx%d_%s" % (
        tex["resource_id"], tex.get("width", 0), tex.get("height", 0),
        safe_name((tex.get("format") or "fmt").lower()))
    path = os.path.join(out_dir, base + "." + ext)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path, "skipped_exists"
    res = tool.save_texture(state.handle, path, file_type=file_type,
                            mip=mip, slice_index=slice_index)
    if not is_success(res):
        raise RuntimeError("SaveTexture failed for %s: %s" %
                           (tex["resource_id"], result_msg(res)))
    return path, "exported"


def write_manifest(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def export_texture(pass_report, out_dir=None, file_type="dds", mip=-1, slice_index=-1,
                   verify_replay=False, config_path=None, verbose=True,
                   renderdoc_dir=None, pymodules_dir=None):
    pass_report = required_value(pass_report, "pass_report", "API argument")
    cfg, cfg_path = load_json_config(config_path)
    dev_dir, pymodules = configure_renderdoc_path(
        cfg, cfg_path, renderdoc_dir=renderdoc_dir, pymodules_dir=pymodules_dir)
    import_rdoc_interface()

    if not out_dir:
        stem, _ = os.path.splitext(pass_report)
        out_dir = stem + "_textures"
    ensure_dir(out_dir)

    rd_file_type = file_type_from_name(file_type)
    ext = texture_file_extension(rd_file_type)

    capture, fmt, passes = parse_pass_report(pass_report)
    textures = unique_textures(passes)
    wanted_ids = set(t["resource_id"] for t in textures)

    if verbose:
        print("[export_texture] config      : %s" % cfg_path)
        print("[export_texture] renderdoc   : %s" % dev_dir)
        print("[export_texture] pymodules   : %s" % pymodules)
        print("[export_texture] pass report : %s" % pass_report)
        print("[export_texture] capture     : %s" % capture)
        print("[export_texture] passes      : %d" % len(passes))
        print("[export_texture] textures    : %d unique" % len(textures))
        print("[export_texture] output      : %s" % out_dir)
        print("[export_texture] type        : %s" % ext)

    t0 = time.time()
    initialise_replay()
    try:
        with CaptureSession(capture) as tool:
            verified_ids = None
            verified_by_eid = None
            if verify_replay:
                if verbose:
                    print("[export_texture] verify      : replaying %d listed passes" % len(passes))
                verified_ids, verified_by_eid = verify_bindings(tool, passes, wanted_ids)
                missing = sorted(wanted_ids - verified_ids)
                if missing:
                    raise RuntimeError("listed textures not currently PS-bound after replay: %s" %
                                       ", ".join(missing))

            exported = []
            errors = []
            for i, tex in enumerate(textures, 1):
                try:
                    state = enrich_from_capture(tool, tex)
                    if state is None:
                        raise RuntimeError("resource is not a texture in capture")
                    path, status = export_one(tool, state, tex, out_dir, ext,
                                              rd_file_type, mip, slice_index)
                    tex["path"] = path
                    tex["status"] = status
                    exported.append(tex)
                    if verbose:
                        print("[export_texture] %2d/%d %-14s %s" %
                              (i, len(textures), status, path))
                except Exception as exc:
                    tex["error"] = str(exc)
                    errors.append(tex)
                    if verbose:
                        print("[export_texture] %2d/%d FAILED       %s: %s" %
                              (i, len(textures), tex["resource_id"], exc))

            manifest = {
                "pass_report": pass_report,
                "capture": capture,
                "format": fmt,
                "output_dir": out_dir,
                "export_type": ext,
                "mip": mip,
                "slice": slice_index,
                "verify_replay": verify_replay,
                "verified_by_eid": verified_by_eid,
                "passes": passes,
                "textures": textures,
                "elapsed_sec": time.time() - t0,
            }
            manifest_path = os.path.join(out_dir, "manifest.json")
            write_manifest(manifest_path, manifest)

            if verbose:
                print("[export_texture] exported    : %d" % len(exported))
                print("[export_texture] errors      : %d" % len(errors))
                print("[export_texture] manifest    : %s" % manifest_path)
                print("[export_texture] elapsed     : %.1fs" % (time.time() - t0))
            if errors:
                return 1
            return manifest
    finally:
        shutdown_replay()


def export(pass_report=None, out_dir=None, file_type=None, mip=None, slice_index=None,
           verify_replay=None, config_path=None, verbose=True, overrides=None,
           renderdoc_dir=None, pymodules_dir=None):
    cfg, cfg_path = load_json_config(config_path)
    ex_cfg = dict(cfg.get("export_texture") or {})
    if overrides:
        ex_cfg.update(overrides)
    if pass_report is None:
        pass_report = ex_cfg.get("pass_report")
    if out_dir is None:
        out_dir = ex_cfg.get("out_dir") or None
    if file_type is None:
        file_type = ex_cfg.get("file_type") or ex_cfg.get("type") or "dds"
    if mip is None:
        mip = int(ex_cfg.get("mip", -1))
    if slice_index is None:
        slice_index = int(ex_cfg.get("slice", -1))
    if verify_replay is None:
        verify_replay = as_bool(ex_cfg.get("verify_replay"), False)
    return export_texture(
        pass_report=required_value(pass_report, "export_texture.pass_report", cfg_path),
        out_dir=out_dir,
        file_type=file_type,
        mip=mip,
        slice_index=slice_index,
        verify_replay=verify_replay,
        config_path=config_path,
        verbose=verbose,
        renderdoc_dir=renderdoc_dir,
        pymodules_dir=pymodules_dir)


def main():
    result = export()
    return 1 if result == 1 else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
