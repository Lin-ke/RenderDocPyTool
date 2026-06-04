# -*- coding: utf-8 -*-
"""
export_pass_debug.py

Dump the COMPLETE per-step shader execution state for a given draw
call (EID) in a RenderDoc capture.

What gets dumped (plain text, RenderDoc-debugger-panel style):
  1. All pipeline bindings at the event (CBs with values, SRVs, samplers,
     UAVs, render targets, viewport).
  2. The shader at that stage (reflection signatures + full disassembly).
  3. Every step of the shader execution (pixel-shader by default):
       - next instruction index
       - source mapping (if any)
       - variable changes (before / after)
       - the full set of live variables at that step

All configuration is passed explicitly via config dicts and function arguments.
No environment variables or global state are used.

The script is meant to be run via:

    qrenderdoc.exe --python export_pass_debug.py

because the `renderdoc` / `qrenderdoc` Python modules are compiled into
qrenderdoc.exe and are not available to a stand-alone Python install.

Runtime parameters (passed as function arguments only):

    rdc_path       path to .rdc            (required)
    eid            event id (int)          (required)
    stage          pixel|vertex|compute    default: pixel
    pixel          (x, y) or "X,Y"         default: viewport center
    group          (x, y, z) or "x,y,z"    default: (0, 0, 0)
    thread         (x, y, z) or "x,y,z"    default: (0, 0, 0)
    out_path       output text file        default: <rdc>.eid<eid>.debug.txt
    out_dir        output folder           default: <out stem>.<stage>
    max_steps      cap on steps            default: 0 (unlimited)

Config keys under ``llm`` in ``rdc_tool.json`` (read internally):

    llm_map_shader bool                   default: false
    llm_mapper_script path                 default: <project>/llm_dxil_hlsl_mapper.py
    llm_model      model name              default: ""
    llm_base_url   API base URL            default: ""
    llm_api_key    API key                 default: ""
    llm_timeout    timeout seconds         default: 900

Config keys under ``hlsl_decompiler`` in ``rdc_tool.json`` (read via decompiler_config):

    tool_dir       directory hint for decompiler tools
    spirv_cross    explicit path to spirv-cross
    dxil_spirv     explicit path to dxil-spirv
    dxbc2dxil      explicit path to dxbc2dxil
"""

from __future__ import print_function

import json
import os
import re
import subprocess
import sys
import traceback

import config
import renderdoc as rd

from RenderDocPyTool.hlsl_decompiler_tool import (decompile_shader,
                                                  extension_for_encoding_name,
                                                  mode_from_encoding_name)
from RenderDocPyTool.rdoc_interface import RenderDocTool, export_pass


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MODULE_DIR)


# ---------------------------------------------------------------------------
# Config helpers (pure functions, no global state)
# ---------------------------------------------------------------------------

def _load_project_config(config_path=None):
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    candidates = [
        config_path,
        os.path.join(os.getcwd(), "rdc_tool.json"),
        os.path.join(here, "rdc_tool.json"),
        os.path.join(root, "rdc_tool.json"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), path
    return {}, candidates[-1]


def _cfg_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _cfg_tuple(value, n, default):
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)):
        parts = [int(x) for x in value]
    else:
        parts = [int(p) for p in str(value).replace(" ", "").split(",") if p != ""]
    while len(parts) < n:
        parts.append(0)
    return tuple(parts[:n])


def _load_llm_config(config_path=None):
    """Load LLM-related settings from the ``llm`` section of rdc_tool.json."""
    cfg, _ = _load_project_config(config_path)
    llm = cfg.get("llm") or {}
    return {
        "llm_map_shader": _cfg_bool(llm.get("llm_map_shader"), False),
        "llm_mapper_script": llm.get("llm_mapper_script") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llm_dxil_hlsl_mapper.py"),
        "llm_model": llm.get("llm_model") or "",
        "llm_base_url": llm.get("llm_base_url") or "",
        "llm_api_key": llm.get("llm_api_key") or "",
        "llm_timeout": int(llm.get("llm_timeout", 900)),
    }


def _load_decompiler_config(config_path=None):
    cfg, _ = _load_project_config(config_path)
    dec = cfg.get("hlsl_decompiler") or {}
    tool_paths = {
        "spirv_cross": dec.get("spirv_cross") or "",
        "dxil_spirv": dec.get("dxil_spirv") or "",
        "dxbc2dxil": dec.get("dxbc2dxil") or "",
    }
    hint = dec.get("tool_dir") or dec.get("tool_hint") or os.path.dirname(os.path.abspath(__file__))
    if not hint:
        for value in tool_paths.values():
            if value:
                hint = os.path.dirname(value)
                break
    return hint, tool_paths


# ---------------------------------------------------------------------------
# Cross-version helpers
# ---------------------------------------------------------------------------

def _is_success(res):
    """OpenFile/OpenCapture return either ResultCode (old) or ResultDetails (new)."""
    if res is None:
        return False
    if hasattr(res, "code"):
        try:
            return res.code == rd.ResultCode.Succeeded
        except AttributeError:
            pass
    try:
        return res == rd.ResultCode.Succeeded
    except AttributeError:
        pass
    try:
        return res == rd.ReplayStatus.Succeeded
    except AttributeError:
        pass
    return False


def _result_msg(res):
    try:
        return res.Message()
    except Exception:
        return str(res)


def _no_preference():
    """Sentinel that means 'no preference' for sample/primitive/view."""
    return getattr(rd.ReplayController, "NoPreference", 0xFFFFFFFF)


def _stage_enum(name):
    name = name.lower()
    if name in ("pixel", "ps", "fragment", "fs"):
        return rd.ShaderStage.Pixel
    if name in ("vertex", "vs"):
        return rd.ShaderStage.Vertex
    if name in ("compute", "cs"):
        return rd.ShaderStage.Compute
    if name in ("geometry", "gs"):
        return rd.ShaderStage.Geometry
    if name in ("hull", "tesscontrol", "hs"):
        return rd.ShaderStage.Hull
    if name in ("domain", "tesseval", "ds"):
        return rd.ShaderStage.Domain
    raise ValueError("unknown shader stage: %s" % name)


def _find_action(actions, eid):
    for a in actions:
        if a.eventId == eid:
            return a
        sub = _find_action(a.children, eid)
        if sub is not None:
            return sub
    return None


def _action_name(action, controller):
    if hasattr(action, "GetName"):
        try:
            return action.GetName(controller.GetStructuredFile())
        except Exception:
            pass
    return getattr(action, "name", "<unnamed>")


def _safe_name(s):
    s = str(s or "unnamed")
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s.strip("._") or "unnamed"


def _ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def _bytes_from_raw(raw):
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    try:
        return bytes(raw)
    except Exception:
        return b""


def _encoding_name(enc):
    try:
        return enc.name
    except Exception:
        return str(enc)


def _encoding_mode(enc):
    name = _encoding_name(enc).lower().replace("shaderencoding.", "")
    return mode_from_encoding_name(name), extension_for_encoding_name(name)


def _source_files_from_debug_info(refl):
    dbg = getattr(refl, "debugInfo", None)
    files = getattr(dbg, "files", None) if dbg is not None else None
    if not files:
        return []
    return [(getattr(f, "filename", "source_%d.hlsl" % i), getattr(f, "contents", ""))
            for i, f in enumerate(files)]


def _pipeline_object(pipe, stage):
    if stage == rd.ShaderStage.Compute:
        try:
            return pipe.GetComputePipelineObject()
        except Exception:
            pass
    return pipe.GetGraphicsPipelineObject()


def _resource_name(controller, rid):
    """Return RenderDoc's resource name for APIs without PipeState.GetShaderName."""
    if not rid:
        return ""
    try:
        for res in controller.GetResources():
            if res.resourceId == rid:
                return getattr(res, "name", "")
    except Exception:
        pass
    return ""


def _resource_label(controller, rid):
    name = _resource_name(controller, rid)
    return "%s (%s)" % (name, rid) if name else str(rid)


def _descriptor_resource(desc):
    if desc is None:
        return None
    try:
        return desc.resource
    except Exception:
        return getattr(desc, "resourceId", None)


def _descriptor_label(controller, obj):
    """Format descriptors/used descriptors as RenderDoc resource names."""
    desc = getattr(obj, "descriptor", obj)
    rid = _descriptor_resource(desc)
    if rid:
        label = _resource_label(controller, rid)
    else:
        label = str(obj)
    try:
        dtype = desc.type.name
        if dtype and dtype != "Unknown":
            label += "  type=%s" % dtype
    except Exception:
        pass
    try:
        ttype = desc.textureType.name
        if ttype and ttype != "Unknown":
            label += "  view=%s" % ttype
    except Exception:
        pass
    return label


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_binary(path, data):
    with open(path, "wb") as f:
        f.write(data or b"")


# ---------------------------------------------------------------------------
# LLM mapping (all parameters passed explicitly)
# ---------------------------------------------------------------------------

def export_dxil_hlsl_llm_map(dxil_lr_path, hlsl_path, out_dir, out,
                             llm_map_shader=False, llm_mapper_script="",
                             llm_model="", llm_base_url="", llm_api_key="",
                             llm_timeout=900):
    out.write("\n" + "=" * 78 + "\n")
    out.write("DXIL LR <-> HLSL LLM MAP\n")
    out.write("=" * 78 + "\n")

    if not llm_map_shader:
        out.write("LLM map: <skipped: llm_map_shader=False>\n")
        return None
    if not dxil_lr_path or not os.path.exists(dxil_lr_path):
        out.write("LLM map: <skipped: no DXIL LR/disassembly text>\n")
        return None
    if not hlsl_path or not os.path.exists(hlsl_path):
        out.write("LLM map: <skipped: no HLSL source>\n")
        return None
    if not llm_mapper_script or not os.path.exists(llm_mapper_script):
        out.write("LLM map: <skipped: mapper not found: %s>\n" % llm_mapper_script)
        return None

    _ensure_dir(out_dir)
    map_path = os.path.join(out_dir, "dxil_hlsl_map.json")
    cmd = [sys.executable or "python", llm_mapper_script,
           "--dxil-ir", dxil_lr_path,
           "--hlsl", hlsl_path,
           "--out", map_path,
           "--direct-full",
           "--timeout", str(llm_timeout),
           "--retries", "0"]
    if llm_model:
        cmd.extend(["--model", llm_model])
    if llm_base_url:
        cmd.extend(["--base-url", llm_base_url])
    if llm_api_key:
        cmd.extend(["--api-key", llm_api_key])

    out.write("DXIL LR : %s\n" % dxil_lr_path)
    out.write("HLSL    : %s\n" % hlsl_path)
    out.write("Output  : %s\n" % map_path)
    out.write("Command : %s\n" % " ".join('"%s"' % c if " " in c else c for c in cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        if stdout:
            out.write("--- mapper stdout ---\n%s\n" % stdout.decode("utf-8", "replace"))
        if stderr:
            out.write("--- mapper stderr ---\n%s\n" % stderr.decode("utf-8", "replace"))
        if proc.returncode != 0:
            out.write("LLM map: <failed: exit %d>\n" % proc.returncode)
            return None
        out.write("LLM map: %s\n" % map_path)
        try:
            data = json.loads(_read_text(map_path))
            out.write("LLM confidence: %s\n" % data.get("confidence", "<unknown>"))
            out.write("LLM mappings  : %d\n" % len(data.get("mappings") or []))
            if data.get("summary"):
                out.write("LLM summary   : %s\n" % data.get("summary"))
        except Exception as e:
            out.write("LLM map summary unavailable: %s\n" % e)
        return map_path
    except Exception as e:
        out.write("LLM map: <failed: %s>\n" % e)
        return None


# ---------------------------------------------------------------------------
# Shader export (all parameters passed explicitly)
# ---------------------------------------------------------------------------

def export_shader_hlsl(shader, out_dir, out, hlsl_decompiler="",
                       hlsl_decompiler_tools=None):
    """Dump HLSL when possible; only dump DXIL/disassembly as fallback evidence."""
    out.write("\n" + "=" * 78 + "\n")
    out.write("HLSL DECOMPILE INPUT\n")
    out.write("=" * 78 + "\n")

    if shader is None or shader.reflection is None:
        out.write("(no reflection)\n")
        return None, [], None

    refl = shader.reflection
    _ensure_dir(out_dir)
    stage_name = _safe_name(getattr(getattr(refl, "stage", None), "name", "shader"))
    shader_base = os.path.join(out_dir, "%s_shader" % stage_name.lower())

    mode, ext = _encoding_mode(getattr(refl, "encoding", None))
    raw_path = shader_base + ext
    dxil_lr_path = shader_base + ".dxil_lr"
    raw = shader.raw_bytes
    hlsl_path = shader_base + ".hlsl"
    fallback_reasons = []

    out.write("Encoding   : %s\n" % _encoding_name(getattr(refl, "encoding", None)))

    source_files = shader.debug_source_files
    if source_files:
        out.write("Debug source files: %d\n" % len(source_files))
        for i, (fname, contents) in enumerate(source_files):
            name = _safe_name(os.path.basename(fname) or ("source_%d.hlsl" % i))
            src_path = os.path.join(out_dir, "source_%02d_%s" % (i, name))
            _write_text(src_path, contents)
            out.write("  source#%d : %s\n" % (i, src_path))
        # Prefer original HLSL/debug source if RenderDoc has it.
        _write_text(hlsl_path, source_files[0][1])
        out.write("HLSL source: %s (from debug info)\n" % hlsl_path)
        return hlsl_path, source_files, None

    if not raw:
        out.write("HLSL source: <unavailable: no raw shader bytes>\n")
        fallback_reasons.append("no raw shader bytes for decompiler")
        return _export_shader_fallback(shader, raw, raw_path, dxil_lr_path, fallback_reasons, out)

    if mode is None:
        out.write("HLSL source: <unavailable: unsupported encoding %s>\n"
                  % _encoding_name(getattr(refl, "encoding", None)))
        fallback_reasons.append("unsupported shader encoding for decompiler")
        return _export_shader_fallback(shader, raw, raw_path, dxil_lr_path, fallback_reasons, out)

    if not hlsl_decompiler:
        out.write("HLSL source: <skipped: no decompiler tool path>\n")
        fallback_reasons.append("no decompiler tool path")
        return _export_shader_fallback(shader, raw, raw_path, dxil_lr_path, fallback_reasons, out)

    _write_binary(raw_path, raw)
    out.write("Decompiler input: %s\n" % raw_path)

    result = decompile_shader(raw_path, _encoding_name(getattr(refl, "encoding", None)),
                              hlsl_path, hlsl_decompiler,
                              tool_paths=hlsl_decompiler_tools)
    if result.ok:
        out.write("HLSL source: %s\n" % hlsl_path)
        try:
            if os.path.exists(raw_path):
                os.remove(raw_path)
        except Exception:
            pass
        try:
            with open(hlsl_path, "r", encoding="utf-8") as f:
                return hlsl_path, [(os.path.basename(hlsl_path), f.read())], None
        except Exception:
            return hlsl_path, [], None
    out.write("--- decompiler log ---\n")
    for line in result.log:
        out.write("%s\n" % line)
    out.write("HLSL source: <decompile failed: exit %d>\n" % result.return_code)
    fallback_reasons.append("decompiler failed with exit %d" % result.return_code)
    return _export_shader_fallback(shader, raw, raw_path, dxil_lr_path, fallback_reasons, out)


def _export_shader_fallback(shader, raw, raw_path, dxil_lr_path, reasons, out):
    out.write("DXIL fallback: HLSL unavailable; writing shader bytes/disassembly for inspection.\n")
    for reason in reasons:
        out.write("  reason: %s\n" % reason)
    if raw:
        _write_binary(raw_path, raw)
        out.write("Raw shader : %s\n" % raw_path)
    else:
        out.write("Raw shader : <not available>\n")
    try:
        disasm = shader.disassemble()
        if disasm:
            _write_text(dxil_lr_path, disasm)
            out.write("DXIL LR    : %s\n" % dxil_lr_path)
            return None, [], dxil_lr_path
        out.write("DXIL LR    : <not available>\n")
    except Exception as e:
        out.write("DXIL LR    : <unavailable: %s>\n" % e)
    return None, [], None


# ---------------------------------------------------------------------------
# ShaderVariable pretty-printing
# ---------------------------------------------------------------------------

def shader_value_str(var, indent=0):
    if var is None:
        return "<None>"
    ind = "  " * indent

    # struct / array -> recurse on members
    try:
        members = list(var.members)
    except Exception:
        members = []
    if members:
        out = "{\n"
        for m in members:
            out += "%s  .%s = %s\n" % (ind, m.name, shader_value_str(m, indent + 1))
        out += "%s}" % ind
        return out

    rows = max(getattr(var, "rows", 0) or 0, 1)
    cols = max(getattr(var, "columns", 0) or 0, 1)
    t = getattr(var, "type", None)

    val = getattr(var, "value", None)
    if val is None:
        return "<no value>"

    def _scalar(i):
        try:
            if t == rd.VarType.Float:  return "%.6g" % val.f32v[i]
            if t == rd.VarType.Double: return "%.10g" % val.f64v[i]
            if t == rd.VarType.Half:
                try:    return "%.6g" % val.f16v[i]
                except: return "%.6g" % val.f32v[i]
            if t == rd.VarType.SInt:   return str(val.s32v[i])
            if t == rd.VarType.UInt:   return str(val.u32v[i])
            if t == rd.VarType.SShort: return str(val.s16v[i])
            if t == rd.VarType.UShort: return str(val.u16v[i])
            if t == rd.VarType.SByte:  return str(val.s8v[i])
            if t == rd.VarType.UByte:  return str(val.u8v[i])
            if t == rd.VarType.SLong:  return str(val.s64v[i])
            if t == rd.VarType.ULong:  return str(val.u64v[i])
            if t == rd.VarType.Bool:   return "true" if val.u32v[i] else "false"
            if t == rd.VarType.GPUPointer:
                return "0x%016x" % val.u64v[i]
        except Exception:
            pass
        # generic fallback: raw u32 + float view
        try:
            u = val.u32v[i]
            f = val.f32v[i]
            return "0x%08x(%.6g)" % (u, f)
        except Exception:
            return "?"

    if rows == 1 and cols == 1:
        return _scalar(0)

    row_strs = []
    for r in range(rows):
        row_strs.append("(" + ", ".join(_scalar(r * cols + c) for c in range(cols)) + ")")

    try:    tn = rd.VarType(t).name
    except: tn = str(t)
    if rows == 1:
        return "%s%d %s" % (tn, cols, row_strs[0])
    return "%s%dx%d [%s]" % (tn, rows, cols, ", ".join(row_strs))


# ---------------------------------------------------------------------------
# Section dumpers
# ---------------------------------------------------------------------------

def dump_bindings(controller, pipe, stage, action, out, tool=None, pass_=None):
    out.write("=" * 78 + "\n")
    out.write("BINDINGS @ EID %d\n" % action.eventId)
    out.write("=" * 78 + "\n")
    out.write("Action            : %s\n" % _action_name(action, controller))
    out.write("Flags             : %s\n" % str(action.flags).replace("ActionFlags.", ""))
    out.write("Indices/Instances : %d / %d\n" % (action.numIndices, action.numInstances))

    # Viewport / Scissor
    out.write("\n[Viewport]\n")
    try:
        vp = pipe.GetViewport(0)
        out.write("  x=%g y=%g w=%g h=%g  z=[%g, %g]\n"
                  % (vp.x, vp.y, vp.width, vp.height, vp.minDepth, vp.maxDepth))
    except Exception as e:
        out.write("  <unavailable: %s>\n" % e)

    # Shader stage info
    shader_id  = pipe.GetShader(stage)
    try:
        shader_nm = pipe.GetShaderName(stage)
    except AttributeError:
        shader_nm = _resource_name(controller, shader_id)
    entry      = pipe.GetShaderEntryPoint(stage)
    out.write("\n[Shader Binding @ %s]\n" % stage.name)
    out.write("  ResourceId : %s\n" % shader_id)
    out.write("  Name       : %s\n" % shader_nm)
    out.write("  EntryPoint : %s\n" % entry)

    refl = pipe.GetShaderReflection(stage)
    if refl is None:
        out.write("  <no reflection>\n")
        return refl, shader_id, entry

    pipeline_id = _pipeline_object(pipe, stage)

    if tool is None:
        tool = RenderDocTool(controller)
    if pass_ is None:
        pass_ = tool.get_pass(action.eventId)

    # Constant buffers
    out.write("\n[Constant Buffers]\n")
    cb_bindings = [b for b in pass_.inputs if b.category == "cbv" and b.stage == stage]
    if not cb_bindings:
        out.write("  (none)\n")
    for i, cb_ref in enumerate(refl.constantBlocks):
        bound = cb_bindings[i] if i < len(cb_bindings) else None
        out.write("  slot %d  %s\n" % (i, cb_ref.name))
        if bound is not None:
            state = tool.descriptor_state(bound)
            out.write("    buffer=%s\n" % state.label())
        else:
            out.write("    buffer=<unbound>\n")
        # Try a few signatures of GetCBufferVariableContents for cross-version safety
        vars_ = None
        last_err = None
        cbuf = bound.descriptor if bound is not None else None
        for args in (
            (pipeline_id, shader_id, stage, entry, i,
             bound.handle if bound is not None else rd.ResourceId.Null(),
             getattr(cbuf, "byteOffset", 0), getattr(cbuf, "byteSize", 0)),
            (shader_id, entry, i, bound.handle if bound is not None else rd.ResourceId.Null(),
             getattr(cbuf, "byteOffset", 0), getattr(cbuf, "byteSize", 0)),
            (shader_id, entry, i, bound.handle if bound is not None else rd.ResourceId.Null(),
             getattr(cbuf, "byteOffset", 0)),
        ):
            try:
                vars_ = controller.GetCBufferVariableContents(*args)
                break
            except Exception as e:
                last_err = e
        if vars_ is None:
            out.write("    <cannot read cbuffer contents: %s>\n" % last_err)
        else:
            for v in vars_:
                out.write("    %s = %s\n" % (v.name, shader_value_str(v, 3)))

    def _dump_bindings(title, bindings):
        out.write("\n[%s]\n" % title)
        if not bindings:
            out.write("  (none)\n"); return
        for b in bindings:
            if b.category == "sampler":
                out.write("  slot=%s[%s]  sampler=%s\n" % (b.index, b.array_element, b.sampler))
            else:
                out.write("  slot=%s[%s]  resource=%s\n" %
                          (b.index, b.array_element, tool.descriptor_state(b).label()))

    _dump_bindings("Read-Only Resources (SRVs / Textures)",
                   [b for b in pass_.inputs if b.category == "srv" and b.stage == stage])
    _dump_bindings("Samplers", [b for b in pass_.samplers if b.stage == stage])
    _dump_bindings("Read-Write Resources (UAVs)",
                   [b for b in pass_.outputs if b.category == "uav" and b.stage == stage])

    # Render targets
    out.write("\n[Render Targets]\n")
    try:
        rts = [b for b in pass_.outputs if b.category == "rtv"]
        if not rts:
            out.write("  (none)\n")
        else:
            for rt in rts:
                out.write("  RT%d  resource=%s\n" % (rt.index, tool.descriptor_state(rt).label()))
        for d in [b for b in pass_.outputs if b.category == "dsv"]:
            out.write("  Depth resource=%s\n" % tool.descriptor_state(d).label())
    except Exception as e:
        out.write("  <unavailable: %s>\n" % e)

    return refl, shader_id, entry


def dump_shader(shader, out):
    out.write("\n" + "=" * 78 + "\n")
    out.write("SHADER REFLECTION + DISASSEMBLY\n")
    out.write("=" * 78 + "\n")
    if shader is None or shader.reflection is None:
        out.write("(no reflection)\n")
        return
    refl = shader.reflection
    out.write("Binding     : %s\n" % shader.label())
    out.write("Stage       : %s\n" % refl.stage.name)
    out.write("EntryPoint  : %s\n" % refl.entryPoint)
    try:    out.write("Encoding    : %s\n" % refl.encoding.name)
    except: pass
    try:
        out.write("DebugFlags  : %s\n" % refl.debugInfo.compileFlags)
    except Exception:
        pass

    out.write("\n--- Inputs ---\n")
    for sig in refl.inputSignature:
        out.write("  %s : %s%d  (%s)\n"
                  % (sig.varName, sig.varType.name, sig.compCount, sig.semanticIdxName))
    out.write("\n--- Outputs ---\n")
    for sig in refl.outputSignature:
        out.write("  %s : %s%d  (%s)\n"
                  % (sig.varName, sig.varType.name, sig.compCount, sig.semanticIdxName))

    out.write("\n--- Disassembly ---\n")
    try:
        disasm = shader.disassemble()
        out.write(disasm)
        if not disasm.endswith("\n"):
            out.write("\n")
    except Exception as e:
        out.write("<disassemble failed: %s>\n" % e)


def _line_lookup(source_files):
    lookup = []
    for fname, contents in source_files or []:
        lookup.append((fname, (contents or "").splitlines()))
    return lookup


def _source_snippet(lookup, src):
    if src is None or src[0] < 0 or src[0] >= len(lookup):
        return ""
    fname, lines = lookup[src[0]]
    start = max(1, src[1] or 1)
    end = max(start, src[2] or start)
    end = min(end, len(lines))
    if start > len(lines):
        return ""
    out = ["// %s:%d-%d" % (fname, start, end)]
    for no in range(start, end + 1):
        out.append("%4d | %s" % (no, lines[no - 1]))
    return "\n".join(out)


def _inst_source_cache(trace):
    entries = []
    inst_info = getattr(trace, "instInfo", None)
    if not inst_info:
        return entries
    for ii in inst_info:
        try:
            li = getattr(ii, "lineInfo", None)
            if li is not None:
                src = (li.fileIndex, li.lineStart, li.lineEnd)
            else:
                src = (getattr(ii, "fileIndex", -1),
                       getattr(ii, "lineStart", 0),
                       getattr(ii, "lineEnd", 0))
            entries.append((int(ii.instruction), src))
        except Exception:
            pass
    entries.sort(key=lambda x: x[0])
    return entries


def _source_for_instruction(entries, inst):
    if not entries:
        return None
    best = None
    for instruction, src in entries:
        if instruction > inst:
            break
        best = src
    return best


def _same_src(a, b):
    if a is None or b is None:
        return a is b
    return a[0] == b[0] and a[1] == b[1] and a[2] == b[2]


def _change_lines(changes):
    lines = []
    for ch in changes:
        bn = ch.before.name
        an = ch.after.name
        if bn and an:
            lines.append("~ %s : %s -> %s" % (an, shader_value_str(ch.before, 3),
                                               shader_value_str(ch.after, 3)))
        elif an:
            lines.append("+ %s = %s" % (an, shader_value_str(ch.after, 3)))
        elif bn:
            lines.append("- %s  (was %s)" % (bn, shader_value_str(ch.before, 3)))
    return lines


def _live_lines(live):
    return ["%s = %s" % (nm, shader_value_str(live[nm], 3)) for nm in sorted(live.keys())]


def _flush_group(group, lookup, out_dir, prefix):
    if not group:
        return
    group_no = group["group_no"]
    path = os.path.join(out_dir, "%s_group_%04d.txt" % (prefix, group_no))
    with open(path, "w", encoding="utf-8") as f:
        f.write("Shader Debug Group %d\n" % group_no)
        f.write("Stage       : %s\n" % group["stage"])
        f.write("Steps       : %d-%d\n" % (group["first_step"], group["last_step"]))
        f.write("Instructions: %d-%d\n" % (group["first_inst"], group["last_inst"]))
        src = group.get("src")
        if src is not None and src[0] >= 0:
            f.write("Source     : file#%d line %d-%d\n" % src)
        f.write("\n--- HLSL / Source Block ---\n")
        snippet = _source_snippet(lookup, src)
        f.write((snippet or "<no source mapping>") + "\n")

        f.write("\n--- Calculation Process ---\n")
        for step in group["steps"]:
            f.write("[Step %d] next_instruction=%d step_index=%d\n"
                    % (step["step"], step["next_instruction"], step["step_index"]))
            if step["flags"]:
                f.write("  flags     : %s\n" % step["flags"])
            if step["callstack"]:
                f.write("  callstack : %s\n" % step["callstack"])
            if step["changes"]:
                for line in step["changes"]:
                    f.write("  %s\n" % line)
            else:
                f.write("  changes   : (none)\n")

        f.write("\n--- Calculation Result ---\n")
        if group["result"]:
            for line in group["result"]:
                f.write("%s\n" % line)
        else:
            f.write("(no variable changes in this group)\n")

        f.write("\n--- Register State After Group ---\n")
        for line in group["live"]:
            f.write("%s\n" % line)
    group["path"] = path


def dump_trace_groups(controller, trace, out_dir, source_files, out, prefix, max_steps=0):
    out.write("\n" + "=" * 78 + "\n")
    out.write("HLSL GROUPED SHADER EXECUTION TRACE\n")
    out.write("=" * 78 + "\n")

    if trace is None or trace.debugger is None:
        out.write("Grouped trace unavailable because debugger did not produce a trace.\n")
        return

    _ensure_dir(out_dir)
    lookup = _line_lookup(source_files)
    inst_entries = _inst_source_cache(trace)

    live = {}
    for v in trace.inputs:
        live[v.name] = v

    index_path = os.path.join(out_dir, "%s_groups_index.txt" % prefix)
    groups = []
    group = None
    group_no = 0
    step = 0
    debugger = trace.debugger

    def start_group(src, state):
        return {
            "group_no": group_no,
            "stage": trace.stage.name,
            "src": src,
            "first_step": step,
            "last_step": step,
            "first_inst": state.nextInstruction,
            "last_inst": state.nextInstruction,
            "steps": [],
            "result": [],
            "live": [],
        }

    try:
        while True:
            states = debugger.ContinueDebug()
            if len(states) == 0:
                break
            for s in states:
                src = _source_for_instruction(inst_entries, s.nextInstruction)
                if group is None:
                    group = start_group(src, s)
                elif not _same_src(group.get("src"), src):
                    group["live"] = _live_lines(live)
                    _flush_group(group, lookup, out_dir, prefix)
                    groups.append(group)
                    group_no += 1
                    group = start_group(src, s)

                changes = _change_lines(s.changes)

                for ch in s.changes:
                    bn = ch.before.name
                    an = ch.after.name
                    if bn and not an:
                        live.pop(bn, None)
                    else:
                        nm = an or bn
                        live[nm] = ch.after

                group["last_step"] = step
                group["last_inst"] = s.nextInstruction
                group["steps"].append({
                    "step": step,
                    "next_instruction": s.nextInstruction,
                    "step_index": getattr(s, "stepIndex", step),
                    "flags": str(getattr(s, "flags", 0)) if getattr(s, "flags", 0) else "",
                    "callstack": " / ".join(getattr(s, "callstack", None) or []),
                    "changes": changes,
                })
                group["result"].extend(changes)

                step += 1
                if max_steps and step >= max_steps:
                    if group is not None:
                        group["live"] = _live_lines(live)
                        _flush_group(group, lookup, out_dir, prefix)
                        groups.append(group)
                    out.write("[Aborted: reached max_steps=%d]\n" % max_steps)
                    raise StopIteration
    except StopIteration:
        pass
    finally:
        try:
            controller.FreeTrace(trace)
        except Exception:
            pass

    if group is not None and (not groups or groups[-1] is not group):
        group["live"] = _live_lines(live)
        _flush_group(group, lookup, out_dir, prefix)
        groups.append(group)

    with open(index_path, "w", encoding="utf-8") as idx:
        idx.write("Shader Debug Group Index\n")
        idx.write("Stage : %s\n" % trace.stage.name)
        idx.write("Count : %d\n\n" % len(groups))
        for g in groups:
            src = g.get("src")
            src_s = "file#%d line %d-%d" % src if src is not None and src[0] >= 0 else "<no source>"
            idx.write("%04d  steps %d-%d  inst %d-%d  %s  %s\n"
                      % (g["group_no"], g["first_step"], g["last_step"],
                         g["first_inst"], g["last_inst"], src_s, g.get("path", "")))

    out.write("Groups written: %d\n" % len(groups))
    out.write("Group index   : %s\n" % index_path)


def dump_trace(controller, trace, out, max_steps=0):
    out.write("\n" + "=" * 78 + "\n")
    out.write("SHADER EXECUTION TRACE  (step-by-step)\n")
    out.write("=" * 78 + "\n")

    if trace is None or trace.debugger is None:
        out.write("Debugger could not produce a trace.  Causes:\n")
        out.write(" * the chosen pixel is not covered by this draw,\n")
        out.write(" * the API/driver does not support shader debugging here,\n")
        out.write(" * the shader stage was not bound.\n")
        return

    out.write("Stage : %s\n" % trace.stage.name)

    # Initial inputs ----------------------------------------------------------
    out.write("\n--- Initial Inputs (v-registers / system values) ---\n")
    for v in trace.inputs:
        out.write("  %s = %s\n" % (v.name, shader_value_str(v, 2)))

    # Constant blocks snapshot the debugger will use --------------------------
    cbs = getattr(trace, "constantBlocks", None)
    if cbs:
        out.write("\n--- Constant Blocks (debugger snapshot) ---\n")
        for cb in cbs:
            out.write("  %s\n" % cb.name)
            for m in cb.members:
                out.write("    %s = %s\n" % (m.name, shader_value_str(m, 3)))

    ros = getattr(trace, "readOnlyResources", None)
    if ros:
        out.write("\n--- Read-Only Resources (debugger view) ---\n")
        for r in ros:
            out.write("  %s = %s\n" % (r.name, shader_value_str(r, 2)))

    sams = getattr(trace, "samplers", None)
    if sams:
        out.write("\n--- Samplers (debugger view) ---\n")
        for s in sams:
            out.write("  %s = %s\n" % (s.name, shader_value_str(s, 2)))

    rws = getattr(trace, "readWriteResources", None)
    if rws:
        out.write("\n--- Read-Write Resources (debugger view) ---\n")
        for r in rws:
            out.write("  %s = %s\n" % (r.name, shader_value_str(r, 2)))

    # Build instruction -> source-location cache ------------------------------
    inst_src = {}
    inst_info = getattr(trace, "instInfo", None)
    if inst_info:
        for ii in inst_info:
            try:
                li = getattr(ii, "lineInfo", None)
                if li is not None:
                    inst_src[ii.instruction] = (li.fileIndex, li.lineStart, li.lineEnd)
                else:
                    inst_src[ii.instruction] = (
                        getattr(ii, "fileIndex", -1),
                        getattr(ii, "lineStart",  0),
                        getattr(ii, "lineEnd",    0),
                    )
            except Exception:
                pass

    # Track live variables ----------------------------------------------------
    live = {}
    for v in trace.inputs:
        live[v.name] = v

    out.write("\n--- Steps ---\n")
    debugger = trace.debugger
    step = 0
    try:
        while True:
            states = debugger.ContinueDebug()
            if len(states) == 0:
                break
            for s in states:
                # Apply changes to the live set
                for ch in s.changes:
                    bn = ch.before.name
                    an = ch.after.name
                    if bn and not an:
                        live.pop(bn, None)
                    else:
                        nm = an or bn
                        live[nm] = ch.after

                out.write("\n[Step %d]  next_instruction=%d  step_index=%d\n"
                          % (step, s.nextInstruction,
                             getattr(s, "stepIndex", step)))

                src = inst_src.get(s.nextInstruction)
                if src is not None and src[0] >= 0:
                    out.write("  src       : file#%d line %d-%d\n" % src)

                fl = getattr(s, "flags", 0)
                if fl:
                    out.write("  flags     : %s\n" % str(fl))

                cs = getattr(s, "callstack", None)
                if cs:
                    out.write("  callstack : %s\n" % " / ".join(cs))

                if s.changes:
                    out.write("  changes (%d):\n" % len(s.changes))
                    for ch in s.changes:
                        bn = ch.before.name
                        an = ch.after.name
                        if bn and an:
                            out.write("    ~ %s : %s -> %s\n"
                                      % (an, shader_value_str(ch.before, 3),
                                             shader_value_str(ch.after,  3)))
                        elif an:
                            out.write("    + %s = %s\n"
                                      % (an, shader_value_str(ch.after, 3)))
                        elif bn:
                            out.write("    - %s  (was %s)\n"
                                      % (bn, shader_value_str(ch.before, 3)))
                else:
                    out.write("  changes   : (none)\n")

                out.write("  live (%d):\n" % len(live))
                for nm in sorted(live.keys()):
                    out.write("    %s = %s\n" % (nm, shader_value_str(live[nm], 3)))

                step += 1
                if max_steps and step >= max_steps:
                    out.write("\n[Aborted: reached max_steps=%d]\n" % max_steps)
                    return
    finally:
        try:    controller.FreeTrace(trace)
        except: pass

    out.write("\n[End of trace - %d steps total]\n" % step)


def debug_compute_trace(controller, out, eid, group, thread):
    out.write("\n[Debug Compute group=%s thread=%s]\n" % (group, thread))
    print("[mirage-debug] DebugThread group=%s thread=%s" % (group, thread))
    try:
        trace = controller.DebugThread(group, thread)
    except TypeError:
        trace = controller.DebugThread(list(group), list(thread))
    prefix = "compute_eid%d_g%d_%d_%d_t%d_%d_%d" % (
        eid, group[0], group[1], group[2], thread[0], thread[1], thread[2])
    return trace, prefix


def debug_pixel_trace(controller, pipe, out, eid, pixel_xy):
    if pixel_xy is None:
        try:
            vp = pipe.GetViewport(0)
            px = max(0, int(vp.x + vp.width  / 2))
            py = max(0, int(vp.y + vp.height / 2))
        except Exception:
            px, py = 0, 0
    else:
        px, py = pixel_xy

    out.write("\n[Debug Pixel x=%d y=%d]\n" % (px, py))
    print("[mirage-debug] DebugPixel x=%d y=%d" % (px, py))

    inputs = rd.DebugPixelInputs()
    npref  = _no_preference()
    for fld in ("sample", "primitive", "view"):
        try:
            setattr(inputs, fld, npref)
        except Exception:
            pass
    trace = controller.DebugPixel(px, py, inputs)
    prefix = "pixel_eid%d_x%d_y%d" % (eid, px, py)
    return trace, prefix


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def export_pass_debug(rdc_path, eid, stage="pixel", pixel_xy=None, group=(0, 0, 0),
                      thread=(0, 0, 0), out_path=None, out_dir=None, max_steps=0,
                      config_path=None, decompiler_config=None):
    """Dump complete shader debug state for a draw call.

    All parameters are passed explicitly. No environment variables or global
    state are used.

    Parameters
    ----------
    rdc_path : str
        Path to the .rdc capture file.
    eid : int
        Event ID to debug.
    stage : str
        Shader stage name (pixel, vertex, compute, etc.).
    pixel_xy : tuple(int, int) or str, optional
        Pixel coordinates for pixel-shader debugging.
    group : tuple(int, int, int) or str, optional
        Compute dispatch group.
    thread : tuple(int, int, int) or str, optional
        Compute thread within group.
    out_path : str, optional
        Explicit output text file path.
    out_dir : str, optional
        Explicit output folder path.
    max_steps : int
        Maximum debug steps (0 = unlimited).
    config_path : str, optional
        Path to rdc_tool.json for loading llm / decompiler defaults.
    decompiler_config : dict, optional
        Decompiler settings (overrides ``hlsl_decompiler`` section in config).
    """
    # Load configs
    hlsl_decompiler, hlsl_decompiler_tools = _load_decompiler_config(config_path)
    if decompiler_config:
        hlsl_decompiler = decompiler_config.get("tool_dir") or hlsl_decompiler
        hlsl_decompiler_tools = decompiler_config.get("tool_paths") or hlsl_decompiler_tools

    llm = _load_llm_config(config_path)
    llm_map_shader = _cfg_bool(llm.get("llm_map_shader"), False)
    llm_mapper_script = llm.get("llm_mapper_script") or os.path.join(PROJECT_ROOT, "llm_dxil_hlsl_mapper.py")
    llm_model = llm.get("llm_model") or ""
    llm_base_url = llm.get("llm_base_url") or ""
    llm_api_key = llm.get("llm_api_key") or ""
    llm_timeout = int(llm.get("llm_timeout", 900))

    stage_str = stage.lower()
    stage = _stage_enum(stage_str)
    out_path = _derive_out_path(rdc_path, eid, out_path or "")
    out_dir = _derive_out_dir(out_path, stage_str, out_dir or "")

    print("[mirage-debug] capture : %s" % rdc_path)
    print("[mirage-debug] eid     : %d" % eid)
    print("[mirage-debug] stage   : %s" % stage.name)
    print("[mirage-debug] out     : %s" % out_path)
    print("[mirage-debug] out dir : %s" % out_dir)

    if stage not in (rd.ShaderStage.Pixel, rd.ShaderStage.Compute):
        raise RuntimeError("export_pass_debug now supports only pixel or compute stages; got %s" % stage.name)

    # InitialiseReplay is required for stand-alone use.  When running under
    # `qrenderdoc --python` the host has not yet initialised, so we do it.
    init_done = False
    try:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        init_done = True
    except Exception as e:
        print("[mirage-debug] InitialiseReplay skipped: %s" % e)

    try:
        print("[mirage-debug] seeking to EID %d ..." % eid)

        def _dump(tool, pass_, controller, pipe, action):
            with open(out_path, "w", encoding="utf-8") as out:
                out.write("RenderDoc Debug Dump\n")
                out.write("Capture : %s\n" % rdc_path)
                out.write("EID     : %d\n" % eid)
                out.write("Stage   : %s\n\n" % stage.name)

                dump_bindings(controller, pipe, stage, action, out, tool, pass_)

                shader = pass_.getShader(stage)
                dump_shader(shader, out)
                hlsl_path, source_files, dxil_lr_path = export_shader_hlsl(
                    shader, out_dir, out,
                    hlsl_decompiler=hlsl_decompiler,
                    hlsl_decompiler_tools=hlsl_decompiler_tools)
                if hlsl_path:
                    out.write("HLSL grouping source: %s\n" % hlsl_path)
                if dxil_lr_path and hlsl_path:
                    try:
                        export_dxil_hlsl_llm_map(
                            dxil_lr_path, hlsl_path, out_dir, out,
                            llm_map_shader=llm_map_shader,
                            llm_mapper_script=llm_mapper_script,
                            llm_model=llm_model,
                            llm_base_url=llm_base_url,
                            llm_api_key=llm_api_key,
                            llm_timeout=llm_timeout)
                    except Exception as _llm_err:
                        out.write("LLM map: <failed: %s>\n" % _llm_err)

                # ------ decide which Debug* to call -------------------
                if stage == rd.ShaderStage.Compute:
                    trace, prefix = debug_compute_trace(controller, out, eid, group, thread)
                else:
                    trace, prefix = debug_pixel_trace(controller, pipe, out, eid, pixel_xy)

                dump_trace_groups(controller, trace, out_dir, source_files, out, prefix, max_steps=max_steps)

        export_pass(rdc_path, eid, _dump, force=True)
        print("[mirage-debug] OK -> %s" % out_path)
        return {"out_path": out_path, "out_dir": out_dir, "rdc_path": rdc_path, "eid": eid,
                "stage": stage.name, "pixel_xy": pixel_xy, "group": group, "thread": thread}
    finally:
        if init_done:
            try: rd.ShutdownReplay()
            except Exception: pass


def _derive_out_path(rdc_path, eid, out_path):
    if out_path:
        return out_path
    base, _ = os.path.splitext(rdc_path)
    return "%s.eid%d.debug.txt" % (base, eid)


def _derive_out_dir(out_path, stage_str, out_dir):
    if out_dir:
        return out_dir
    base, _ = os.path.splitext(out_path)
    return "%s.%s" % (base, stage_str)


def export(config_path=None, overrides=None):
    """Convenience entry point that reads defaults from ``export_pass_debug``
    section of ``rdc_tool.json``.

    .. deprecated::
        Prefer calling ``export_pass_debug()`` directly with explicit arguments.
    """
    cfg, _ = _load_project_config(config_path)
    ep = cfg.get("export_pass_debug") or {}
    if overrides:
        ep.update(overrides)
    return export_pass_debug(
        rdc_path=ep.get("capture") or r"E:\mirage\mirage1.rdc",
        eid=int(ep.get("eid", 8935)),
        stage=str(ep.get("stage") or "pixel").lower(),
        pixel_xy=_cfg_tuple(ep.get("pixel"), 2, None),
        group=_cfg_tuple(ep.get("group"), 3, (0, 0, 0)),
        thread=_cfg_tuple(ep.get("thread"), 3, (0, 0, 0)),
        out_path=ep.get("out") or None,
        out_dir=ep.get("out_dir") or None,
        max_steps=int(ep.get("max_steps", 0)),
        config_path=config_path)


def main():
    return export()


if __name__ == "__main__":
    try:
        main()
        # When invoked via `qrenderdoc --python`, qrenderdoc keeps going and
        # tries to open its main window after our script returns.  We hard-exit
        # so the process terminates cleanly in headless mode.
        os._exit(0)
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        os._exit(1)
