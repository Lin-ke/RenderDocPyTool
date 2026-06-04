# -*- coding: utf-8 -*-
"""Small wrapper around the tools used by YYadorigi/HLSL-Decompiler.

The project itself only chains existing executables:
  DXBC  -> dxbc2dxil -> dxil-spirv -> spirv-cross -> HLSL
  DXIL  -> dxil-spirv -> spirv-cross -> HLSL
  SPIRV -> spirv-cross -> HLSL

This module keeps that process outside RenderDoc dump scripts so it can be
used both as an import and as a small command-line tool.

All configuration is passed explicitly via function arguments.
No environment variables or global state are used.
"""

from __future__ import print_function

import argparse
import os
import subprocess
import sys


class DecompileResult(object):
    def __init__(self, ok, output_path, log, return_code=0):
        self.ok = ok
        self.output_path = output_path
        self.log = log
        self.return_code = return_code


def _norm_encoding(s):
    return str(s or "").lower().replace("shaderencoding.", "").replace("-", "")


def mode_from_encoding_name(encoding_name):
    name = _norm_encoding(encoding_name)
    if name == "dxbc":
        return "-dxbc"
    if name == "dxil":
        return "-dxil"
    if name in ("spirv", "openglspirv"):
        return "-spirv"
    if name in ("spvasm", "spirvasm", "openglspirvasm"):
        return None
    return None


def extension_for_encoding_name(encoding_name):
    mode = mode_from_encoding_name(encoding_name)
    if mode == "-dxbc":
        return ".dxbc"
    if mode == "-dxil":
        return ".dxil"
    if mode == "-spirv":
        return ".spv"
    return ".bin"


def _exe_name(name):
    return name + ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else name


def _resolve_tool_hint(tool_hint):
    if not tool_hint:
        return "", ""
    tool_hint = os.path.abspath(tool_hint)
    if os.path.isdir(tool_hint):
        return tool_hint, ""
    return os.path.dirname(tool_hint), tool_hint


def _tool_path(tool_dir, name):
    if not tool_dir:
        return ""
    exe = _exe_name(name)
    candidates = [
        os.path.join(tool_dir, exe),
        os.path.join(tool_dir, "Release", exe),
        os.path.join(tool_dir, "vendor", "dxil-spirv", "Release", exe),
        os.path.join(tool_dir, "vendor", "SPIRV-Cross", "Release", exe),
        os.path.join(tool_dir, "vendor", "DirectXShaderCompiler", "Release", "bin", exe),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def _explicit_tool_path(tool_paths, key, fallback_name=None):
    if not tool_paths:
        return ""
    value = tool_paths.get(key) or (tool_paths.get(fallback_name) if fallback_name else "")
    if value and os.path.exists(value):
        return os.path.abspath(value)
    return ""


def _run(args, cwd, log):
    log.append("$ " + " ".join('"%s"' % a if " " in a else a for a in args))
    p = subprocess.Popen(args, cwd=cwd or None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = p.communicate()
    if stdout:
        log.append(stdout.decode("utf-8", "replace"))
    if stderr:
        log.append(stderr.decode("utf-8", "replace"))
    if p.returncode != 0:
        raise RuntimeError("command failed with exit code %d" % p.returncode)
    return p.returncode


def _call_wrapper(wrapper_path, input_path, mode, output_path, log):
    _run([wrapper_path, input_path, mode, output_path], os.path.dirname(wrapper_path), log)


def _call_direct(tool_dir, input_path, mode, output_path, keep_intermediate, log, tool_paths=None):
    dxbc2dxil = _explicit_tool_path(tool_paths, "dxbc2dxil") or _tool_path(tool_dir, "dxbc2dxil")
    dxil_spirv = _explicit_tool_path(tool_paths, "dxil_spirv", "dxil-spirv") or _tool_path(tool_dir, "dxil-spirv")
    spirv_cross = _explicit_tool_path(tool_paths, "spirv_cross", "spirv-cross") or _tool_path(tool_dir, "spirv-cross")

    missing = []
    if mode == "-dxbc" and not dxbc2dxil:
        missing.append(_exe_name("dxbc2dxil"))
    if mode in ("-dxbc", "-dxil") and not dxil_spirv:
        missing.append(_exe_name("dxil-spirv"))
    if not spirv_cross:
        missing.append(_exe_name("spirv-cross"))
    if missing:
        raise RuntimeError("missing tools in %s: %s" % (tool_dir, ", ".join(missing)))

    stem = os.path.splitext(output_path)[0]
    temp_dxil = stem + ".tmp.dxil"
    temp_spv = stem + ".tmp.spv"
    temps = []

    try:
        if mode == "-dxbc":
            temps.extend([temp_dxil, temp_spv])
            _run([dxbc2dxil, input_path, "-o", temp_dxil, "-emit-bc"], tool_dir, log)
            _run([dxil_spirv, temp_dxil, "--output", temp_spv, "--raw-llvm"], tool_dir, log)
            _run([spirv_cross, temp_spv, "--output", output_path, "--hlsl", "--shader-model", "50"], tool_dir, log)
        elif mode == "-dxil":
            temps.append(temp_spv)
            _run([dxil_spirv, input_path, "--output", temp_spv], tool_dir, log)
            _run([spirv_cross, temp_spv, "--output", output_path, "--hlsl", "--shader-model", "60"], tool_dir, log)
        elif mode == "-spirv":
            _run([spirv_cross, input_path, "--output", output_path, "--hlsl", "--shader-model", "60"], tool_dir, log)
        else:
            raise RuntimeError("unsupported mode: %s" % mode)
    finally:
        if not keep_intermediate:
            for path in temps:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass


def decompile_shader(input_path, encoding_name, output_path, tool_hint, keep_intermediate=False,
                     tool_paths=None):
    """Decompile a raw shader file to HLSL.

    tool_hint can be either the HLSL-Decompiler build directory, or a path to
    HLSLDecompiler.bat/.exe. If the component tools are present, they are called
    directly; otherwise the wrapper is called as a fallback.
    """
    log = []
    mode = mode_from_encoding_name(encoding_name)
    if mode is None:
        return DecompileResult(False, output_path,
                               ["unsupported shader encoding: %s" % encoding_name], 1)

    tool_dir, wrapper = _resolve_tool_hint(tool_hint)
    if not tool_dir:
        return DecompileResult(False, output_path, ["no tool path provided"], 1)

    try:
        if not os.path.isdir(os.path.dirname(os.path.abspath(output_path))):
            os.makedirs(os.path.dirname(os.path.abspath(output_path)))

        has_direct = bool(_explicit_tool_path(tool_paths, "spirv_cross", "spirv-cross") or
                          _tool_path(tool_dir, "spirv-cross"))
        if mode in ("-dxbc", "-dxil"):
            has_direct = has_direct and bool(_explicit_tool_path(tool_paths, "dxil_spirv", "dxil-spirv") or
                                             _tool_path(tool_dir, "dxil-spirv"))
        if mode == "-dxbc":
            has_direct = has_direct and bool(_explicit_tool_path(tool_paths, "dxbc2dxil") or
                                             _tool_path(tool_dir, "dxbc2dxil"))

        if has_direct:
            _call_direct(tool_dir, os.path.abspath(input_path), mode,
                         os.path.abspath(output_path), keep_intermediate, log, tool_paths)
        elif wrapper and os.path.exists(wrapper):
            _call_wrapper(wrapper, os.path.abspath(input_path), mode, os.path.abspath(output_path), log)
        else:
            raise RuntimeError("no usable tools found in %s" % tool_dir)

        ok = os.path.exists(output_path)
        if not ok:
            log.append("output was not created: %s" % output_path)
        return DecompileResult(ok, output_path, log, 0 if ok else 1)
    except Exception as e:
        log.append(str(e))
        return DecompileResult(False, output_path, log, 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Decompile DXBC/DXIL/SPIR-V to HLSL")
    parser.add_argument("input")
    parser.add_argument("mode", choices=["dxbc", "dxil", "spirv"])
    parser.add_argument("output")
    parser.add_argument("--tool", default="",
                        help="HLSL-Decompiler build dir or HLSLDecompiler.bat/.exe")
    parser.add_argument("--keep-intermediate", action="store_true")
    args = parser.parse_args(argv)

    enc = {"dxbc": "DXBC", "dxil": "DXIL", "spirv": "SPIRV"}[args.mode]
    result = decompile_shader(args.input, enc, args.output, args.tool, args.keep_intermediate)
    for line in result.log:
        print(line)
    return 0 if result.ok else result.return_code or 1


if __name__ == "__main__":
    sys.exit(main())
