# -*- coding: utf-8 -*-
r"""Find passes whose pixel shader binds multiple textures of a given format.

Configuration is driven entirely by ``rdc_tool.json`` (no environment
variables, no command-line flags). Edit the config to change captures or
search parameters and re-run from the repository root::

  set PATH=<renderdoc>\x64\Development;<renderdoc>\x64\Development\pymodules;%PATH%
  set PYTHONPATH=<renderdoc>\x64\Development\pymodules
  python examples\search_texture.py

Relevant config keys::

  captures                  list of .rdc paths to scan in order
  search_texture.format     format substring (case-insensitive)
  search_texture.min_textures   minimum matching textures per pass
  search_texture.limit      0 = all candidates; >0 caps verification count
  search_texture.out_dir    output directory; null = next to the .rdc
  search_texture.out_suffix output filename suffix
"""

from __future__ import print_function

import os
import sys
import time
import traceback

# Allow running as `python examples/search_texture.py` from the repo root by
# putting the project root on sys.path so the ``rdoc_tool`` module imports.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import renderdoc as rd

from rdoc_tool import (CaptureSession, flatten_actions, format_name, is_draw,
                       is_shader_read_usage, load_config)


def matching_textures(pass_, needle):
    needle = needle.lower()
    return [t for t in pass_.getPixelTextures() if needle in t.format.lower()]


def progress(done, total):
    total = max(total, 1)
    width = 32
    filled = int(width * done / total)
    bar = "#" * filled + "-" * (width - filled)
    pct = 100.0 * done / total
    sys.stdout.write("\r[search_texture] [%s] %5.1f%%  %d/%d" % (bar, pct, done, total))
    sys.stdout.flush()


def derive_out_path(rdc_path, fmt, min_textures, out_dir, out_suffix):
    base, _ = os.path.splitext(rdc_path)
    if out_dir:
        base = os.path.join(out_dir, os.path.basename(base))
    fmt_slug = fmt.lower().replace("_", "")
    suffix = out_suffix or "passes"
    if min_textures != 2:
        return "%s.%s_min%d_%s.txt" % (base, fmt_slug, min_textures, suffix)
    return "%s.%s_%s.txt" % (base, fmt_slug, suffix)


def run_one(rdc_path, fmt, min_textures, limit, out_path):
    print("[search_texture] capture      : %s" % rdc_path)
    print("[search_texture] format       : %s" % fmt)
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
        #    to PS specifically (D3D12 reports All_Resource so we still need
        #    to look at the pipeline state).
        all_actions = flatten_actions(tool.controller.GetRootActions())
        cand_actions = [a for a in all_actions
                        if a.eventId in candidates and is_draw(a)]
        if limit > 0:
            cand_actions = cand_actions[:limit]
        total = len(cand_actions)
        print("[search_texture] verify draws : %d (limit=%d)" % (total, limit))

        results = []
        t0 = time.time()
        last_print = 0.0
        progress(0, total)
        for i, pass_ in enumerate(tool.iter_passes(cand_actions, action_filter=None), 1):
            matches = matching_textures(pass_, fmt)
            if len(matches) >= min_textures:
                results.append((pass_.action, matches))
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


def main():
    cfg, cfg_path = load_config()
    print("[search_texture] config       : %s" % cfg_path)

    captures = list(cfg.get("captures") or [])
    if not captures:
        print("[search_texture] config has no 'captures' list", file=sys.stderr)
        sys.exit(2)

    st_cfg = cfg.get("search_texture") or {}
    fmt = st_cfg.get("format", "BC1_UNorm")
    min_textures = int(st_cfg.get("min_textures", 2))
    limit = int(st_cfg.get("limit", 0))
    out_dir = st_cfg.get("out_dir") or None
    out_suffix = st_cfg.get("out_suffix") or "passes"

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        for i, rdc_path in enumerate(captures, 1):
            print("==========  [%d/%d] %s  ==========" %
                  (i, len(captures), os.path.basename(rdc_path)))
            out_path = derive_out_path(rdc_path, fmt, min_textures, out_dir, out_suffix)
            run_one(rdc_path, fmt, min_textures, limit, out_path)
            print("")
    finally:
        rd.ShutdownReplay()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
