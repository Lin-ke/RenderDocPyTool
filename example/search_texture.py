# -*- coding: utf-8 -*-
r"""Find passes whose pixel shader binds multiple textures of a given format.

Capture and RenderDoc paths are read from ``rdc_tool.json``. Search parameters
are passed by callers through ``search_texture(...)``:

  python search_texture.py --config path/to/rdc_tool.json

No environment variables or global state are used. All paths and parameters
are passed explicitly via config dict or function arguments.
"""

from __future__ import print_function

import json
import os
import sys
import time
import traceback

import renderdoc as rd

from RenderDocPyTool.rdoc_interface import (CaptureSession, flatten_actions,
                                            format_name, is_draw, is_null_handle,
                                            is_shader_read_usage,
                                            is_texture_descriptor, load_config)


def progress(done, total):
    total = max(total, 1)
    width = 32
    filled = int(width * done / total)
    bar = "#" * filled + "-" * (width - filled)
    pct = 100.0 * done / total
    sys.stdout.write("\r[search_texture] [%s] %5.1f%%  %d/%d" % (bar, pct, done, total))
    sys.stdout.flush()


def cfg_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def parse_shader_stage(value):
    text = str(value or "pixel").strip().lower()
    aliases = {
        "ps": rd.ShaderStage.Pixel,
        "pixel": rd.ShaderStage.Pixel,
        "vs": rd.ShaderStage.Vertex,
        "vertex": rd.ShaderStage.Vertex,
        "cs": rd.ShaderStage.Compute,
        "compute": rd.ShaderStage.Compute,
        "gs": rd.ShaderStage.Geometry,
        "geometry": rd.ShaderStage.Geometry,
        "hs": rd.ShaderStage.Hull,
        "hull": rd.ShaderStage.Hull,
        "ds": rd.ShaderStage.Domain,
        "domain": rd.ShaderStage.Domain,
        "ms": getattr(rd.ShaderStage, "Mesh", None),
        "mesh": getattr(rd.ShaderStage, "Mesh", None),
        "all": None,
        "any": None,
    }
    if text not in aliases:
        raise ValueError("unknown stage: %s" % value)
    return aliases[text]


def shader_stage_label(stage):
    return "All" if stage is None else str(stage).split(".")[-1]


def derive_out_path(rdc_path, fmt, min_textures, out_dir, out_suffix):
    base, _ = os.path.splitext(rdc_path)
    if out_dir:
        base = os.path.join(out_dir, os.path.basename(base))
    fmt_slug = fmt.lower().replace("_", "")
    suffix = out_suffix or "passes"
    if min_textures != 2:
        return "%s.%s_min%d_%s.txt" % (base, fmt_slug, min_textures, suffix)
    return "%s.%s_%s.txt" % (base, fmt_slug, suffix)


def candidate_draws_from_usage(actions, candidates):
    return [a for a in actions if a.eventId in candidates and is_draw(a)]


def is_execute_indirect_child(action):
    if not is_draw(action):
        return False
    try:
        if bool(getattr(action, "flags", 0) & rd.ActionFlags.Indirect):
            return True
    except Exception:
        pass
    parent = getattr(action, "parent", None)
    try:
        return bool(getattr(parent, "flags", 0) & rd.ActionFlags.MultiAction)
    except Exception:
        return False


def matching_fast_stage_textures(tool, action, needle, stage):
    tool.controller.SetFrameEvent(action.eventId, False)
    pipe = tool.controller.GetPipelineState()
    if stage is not None and is_null_handle(pipe.GetShader(stage)):
        return []

    if stage is None:
        used = []
        for s in (rd.ShaderStage.Vertex, rd.ShaderStage.Pixel,
                  rd.ShaderStage.Compute, rd.ShaderStage.Geometry,
                  rd.ShaderStage.Hull, rd.ShaderStage.Domain):
            try:
                used.extend(pipe.GetReadOnlyResources(s, False))
            except TypeError:
                used.extend(pipe.GetReadOnlyResources(s))
            except Exception:
                pass
    else:
        try:
            used = pipe.GetReadOnlyResources(stage, False)
        except TypeError:
            used = pipe.GetReadOnlyResources(stage)

    out = []
    seen = set()
    for used_desc in used:
        access = getattr(used_desc, "access", None)
        if stage is not None and access is not None and getattr(access, "stage", None) != stage:
            continue
        desc = getattr(used_desc, "descriptor", None)
        if not is_texture_descriptor(desc):
            continue
        if needle not in format_name(getattr(desc, "format", "")).lower():
            continue
        handle = getattr(desc, "resource", None)
        if is_null_handle(handle):
            continue
        key = str(handle)
        if key in seen:
            continue
        seen.add(key)
        state = tool.getTexture(handle)
        if state is not None:
            state.descriptor = desc
            out.append(state)
    return out


def merge_execute_indirect_children(actions, base_candidates, descriptor_scan_limit=0):
    candidates = list(base_candidates)
    candidate_ids = set(a.eventId for a in candidates)
    indirect_children = [a for a in actions
                         if a.eventId not in candidate_ids and is_execute_indirect_child(a)]
    if descriptor_scan_limit > 0:
        indirect_children = indirect_children[:descriptor_scan_limit]
    candidates.extend(indirect_children)
    print("[search_texture] indirect scan : +%d child draws (limit=%d)" %
          (len(indirect_children), descriptor_scan_limit))
    return sorted(candidates, key=lambda a: a.eventId)


def run_one(rdc_path, fmt, min_textures, limit, out_path,
            stage=rd.ShaderStage.Pixel, scan_execute_indirect=True,
            descriptor_scan_limit=0):
    print("[search_texture] capture      : %s" % rdc_path)
    print("[search_texture] format       : %s" % fmt)
    print("[search_texture] stage        : %s" % shader_stage_label(stage))
    print("[search_texture] min textures : %d" % min_textures)
    print("[search_texture] output       : %s" % out_path)

    needle = fmt.lower()
    with CaptureSession(rdc_path) as tool:
        t_start = time.time()

        # 1. Pick textures whose format matches; snapshot lookup, no replay.
        t0 = time.time()
        matched_texs = tool.filter_textures(
            lambda t: needle in format_name(t.format).lower())
        t_filter = time.time() - t0
        print("[search_texture] matched tex  : %d (filter %.2fs)" %
              (len(matched_texs), t_filter))

        # 2. Reverse index via GetUsage -- candidate events that read >=
        #    min_textures of those textures in any shader stage.
        t0 = time.time()
        by_event = tool.find_events_using(matched_texs,
                                          usage_filter=is_shader_read_usage)
        candidates = {eid for eid, rids in by_event.items()
                      if len(rids) >= min_textures}
        t_usage = time.time() - t0
        print("[search_texture] candidates   : %d events (GetUsage %.2fs)" %
              (len(candidates), t_usage))

        # 3. Verify each candidate is actually a draw with the textures bound
        #    to the requested shader stage. GetUsage can miss ExecuteIndirect
        #    children, so include them directly instead of descriptor-heap probing.
        all_actions = flatten_actions(tool.controller.GetRootActions())
        cand_actions = candidate_draws_from_usage(all_actions, candidates)
        if scan_execute_indirect:
            cand_actions = merge_execute_indirect_children(
                all_actions, cand_actions, descriptor_scan_limit=descriptor_scan_limit)
        if limit > 0:
            cand_actions = cand_actions[:limit]
        total = len(cand_actions)
        print("[search_texture] verify draws : %d (limit=%d)" % (total, limit))

        results = []
        t0 = time.time()
        last_print = 0.0
        progress(0, total)
        for i, action in enumerate(cand_actions, 1):
            matches = matching_fast_stage_textures(tool, action, needle, stage)
            if len(matches) >= min_textures:
                results.append((action, matches))
            now = time.time()
            if now - last_print >= 1.0 or i == total:
                progress(i, total)
                last_print = now
        sys.stdout.write("\n")
        sys.stdout.flush()
        t_verify = time.time() - t0
        print("[search_texture] verify       : %.1fs" % t_verify)
        print("[search_texture] total elapsed: %.1fs" % (time.time() - t_start))

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("Search Texture Results\n")
            f.write("Capture      : %s\n" % rdc_path)
            f.write("Format       : %s\n" % fmt)
            f.write("Min textures : %d\n" % min_textures)
            f.write("Matches      : %d\n\n" % len(results))

            sfile = tool.controller.GetStructuredFile()
            for action, matches in results:
                name = action.GetName(sfile) if hasattr(action, "GetName") else getattr(action, "name", "")
                f.write("EID %d  count=%d  %s\n" % (action.eventId, len(matches), name))
                for tex in matches:
                    f.write("  - %s\n" % tex.label())
                f.write("\n")

        print("[search_texture] matches: %d" % len(results))
        print("[search_texture] OK -> %s" % out_path)


def search_texture(format="BC1_UNorm", min_textures=4, limit=0, out_dir=None,
                   out_suffix="passes", scan_execute_indirect=True,
                   descriptor_scan_limit=0, stage="pixel", captures=None,
                   config_path=None):
    cfg, cfg_path = load_config(config_path)
    print("[search_texture] config       : %s" % cfg_path)

    captures = list(captures or cfg.get("captures") or [])
    if not captures:
        print("[search_texture] config has no 'captures' list", file=sys.stderr)
        sys.exit(2)

    fmt = format
    stage = parse_shader_stage(stage)
    min_textures = int(min_textures)
    limit = int(limit)
    out_dir = out_dir or None
    out_suffix = out_suffix or "passes"
    scan_execute_indirect = cfg_bool(scan_execute_indirect, True)
    descriptor_scan_limit = int(descriptor_scan_limit)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        for i, rdc_path in enumerate(captures, 1):
            print("==========  [%d/%d] %s  ==========" %
                  (i, len(captures), os.path.basename(rdc_path)))
            out_path = derive_out_path(rdc_path, fmt, min_textures, out_dir, out_suffix)
            run_one(rdc_path, fmt, min_textures, limit, out_path,
                    stage=stage,
                    scan_execute_indirect=scan_execute_indirect,
                    descriptor_scan_limit=descriptor_scan_limit)
            print("")
    finally:
        rd.ShutdownReplay()


def main():
    return search_texture()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
