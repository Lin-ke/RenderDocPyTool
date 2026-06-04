# -*- coding: utf-8 -*-
r"""Map decompiled HLSL statements back to DXIL IR with an LLM.

The script sends line-numbered DXIL IR and line-numbered decompiled HLSL to an
OpenAI-compatible chat-completions API, then writes a JSON relationship map.

All configuration is passed explicitly via function arguments and config dicts.
No environment variables or global state are used.

Example:
    python llm_dxil_hlsl_mapper.py \
      --dxil-ir export_smoke.debug.txt \
      --hlsl export_smoke.pixel\pixel_shader.hlsl \
      --out export_smoke.pixel\dxil_hlsl_map.json \
      --api-key <your-key>
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
import time

try:
    from urllib import request as urlrequest
    from urllib import error as urlerror
except ImportError:  # pragma: no cover - Python 2 fallback for embedded tools
    import urllib2 as urlrequest
    import urllib2 as urlerror


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_API_KEY = ""


SYSTEM_PROMPT = """You are a senior shader compiler engineer.
Your task is to align decompiled HLSL statements with the original DXIL LLVM IR
statements. Be conservative: only create a mapping when the semantic evidence is
clear. Return JSON only, with no markdown or surrounding explanation."""


USER_PROMPT_TEMPLATE = """We need to build a relationship map between DXIL IR and decompiled HLSL.

Context:
- The HLSL was decompiled from the DXIL, so variable names can differ.
- DXIL SSA names such as `%123` or `_123` may correspond to HLSL temporaries such as `_123`, but do not rely on names alone.
- Match by semantics: inputs, outputs, resource declarations, constant-buffer loads, texture loads/samples, arithmetic expressions, conversions, bit operations, branches, phi/select-like merges, and stores.
- If multiple DXIL lines implement one HLSL statement, use a line range.
- If one DXIL line contributes to several HLSL statements, repeat it in multiple mappings.
- If a statement is only boilerplate/declaration with no clear DXIL counterpart, omit it or put it under unmatched_hlsl_lines.
- Prefer precise small mappings over broad vague mappings.

Return a single JSON object with exactly this shape:
{{
  "summary": "short description of the mapping quality",
  "confidence": "high | medium | low",
  "mappings": [
    {{
      "hlsl_lines": [start_line, end_line],
      "hlsl_code": "short copied HLSL excerpt",
      "dxil_lines": [start_line, end_line],
      "dxil_code": "short copied DXIL excerpt",
      "kind": "declaration | input | output | cbuffer | resource | texture_load | texture_sample | arithmetic | bit_op | conversion | control_flow | merge | other",
      "reason": "why these lines correspond",
      "confidence": 0.0
    }}
  ],
  "unmatched_hlsl_lines": [[start_line, end_line]],
  "unmatched_dxil_lines": [[start_line, end_line]]
}}

Use the line numbers shown in the code blocks below.

DXIL IR:
```llvm
{dxil_ir}
```

Decompiled HLSL:
```hlsl
{hlsl_code}
```
"""


DIRECT_PROMPT_TEMPLATE = """建立反编译 HLSL 和 DXIL LR 的语句/代码段对应关系。

要求：
1. 直接阅读完整两个文件，不要说上下文不够。
2. 按语义建立关系，不要只按变量名；但相同/相近临时变量名可以作为辅助证据。
3. 输出 JSON，不要 markdown。
4. 必须覆盖 HLSL 文件：每一行非空、非纯括号/结构声明的 HLSL，都必须出现在 mappings 或 unmatched_hlsl_lines 中。
5. 不要只给摘要式大块；长表达式可以按连续代码段映射，但不能遗漏中间大段。
6. 每条 mapping 包含：hlsl_lines、dxil_lines、kind、confidence、reason。

JSON 结构：
{{
  "summary": "...",
  "confidence": "high|medium|low",
  "mappings": [
    {{"hlsl_lines": [1, 2], "dxil_lines": [3, 4], "kind": "...", "confidence": 0.9, "reason": "..."}}
  ],
  "unmatched_hlsl_lines": [[10, 12]],
  "unmatched_dxil_lines": [[20, 25]],
  "coverage_report": {{"covered_hlsl_line_ranges": [[1, 9]], "uncovered_hlsl_line_ranges": [[10, 12]]}},
  "notes": ["..."]
}}

DXIL LR 完整文件如下：
```llvm
{dxil_ir}
```

HLSL 完整文件如下：
```hlsl
{hlsl_code}
```
"""


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if data.count(b"\x00") > max(4, len(data) // 100):
        raise ValueError("%s looks binary; pass textual DXIL IR/disassembly, not .dxil bitcode" % path)
    for enc in ("utf-8-sig", "utf-16", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def extract_renderdoc_disassembly(text):
    """Return (dxil_text, first_line_number, extracted_from_renderdoc)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "--- Disassembly ---":
            start = i + 1
            break
    if start is None:
        return text, 1, False

    while start < len(lines) and not lines[start].strip():
        start += 1

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip() == "HLSL DECOMPILE INPUT":
            end = max(start, i - 1)
            break
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end]), start + 1, True


def add_line_numbers(text, first_line):
    lines = text.splitlines()
    last_line = first_line + max(0, len(lines) - 1)
    width = max(len(str(last_line)), 4)
    numbered = []
    for i, line in enumerate(lines):
        numbered.append(("%" + str(width) + "d: %s") % (first_line + i, line))
    return "\n".join(numbered)


def clip_text(name, text, max_chars, warnings):
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep_head = max_chars // 2
    keep_tail = max_chars - keep_head
    warnings.append("%s was clipped from %d to %d characters" % (name, len(text), max_chars))
    return text[:keep_head] + "\n... <clipped> ...\n" + text[-keep_tail:]


def build_prompt(dxil_text, dxil_first_line, hlsl_text, max_chars):
    warnings = []
    dxil_numbered = add_line_numbers(dxil_text, dxil_first_line)
    hlsl_numbered = add_line_numbers(hlsl_text, 1)
    dxil_numbered = clip_text("DXIL IR", dxil_numbered, max_chars, warnings)
    hlsl_numbered = clip_text("HLSL", hlsl_numbered, max_chars, warnings)
    prompt = USER_PROMPT_TEMPLATE.format(dxil_ir=dxil_numbered, hlsl_code=hlsl_numbered)
    return prompt, warnings


def build_direct_prompt(dxil_text, dxil_first_line, hlsl_text):
    dxil_numbered = add_line_numbers(dxil_text, dxil_first_line)
    hlsl_numbered = add_line_numbers(hlsl_text, 1)
    return DIRECT_PROMPT_TEMPLATE.format(dxil_ir=dxil_numbered, hlsl_code=hlsl_numbered)


def line_window(text, first_line, start_line, line_count):
    lines = text.splitlines()
    rel_start = max(0, start_line - first_line)
    rel_end = min(len(lines), rel_start + line_count)
    return "\n".join(lines[rel_start:rel_end]), first_line + rel_start


def hlsl_anchor_names(hlsl_chunk):
    names = set(re.findall(r"\b_[0-9]+\b|\bSV_Target(?:_[0-9]+)?\b|\bTEXCOORD(?:_[0-9]+)?\b|\bgl_FragCoord\b|\bInstanceID\b", hlsl_chunk))
    return names


def score_dxil_line(line, names):
    score = 0
    for name in names:
        if name in line:
            score += 10
    if any(word in line for word in ("Sample", "SampleBias", "Load", "Store", "_OUT.", "_IN.", "cbuffer", "Texture2D", "SamplerState", "ByteAddressBuffer")):
        score += 3
    if any(word in line for word in ("SV_Target", "TEXCOORD", "InstanceID", "SV_Position")):
        score += 4
    return score


def related_dxil_for_hlsl_chunk(dxil_text, dxil_first_line, hlsl_chunk, max_lines):
    dxil_lines = dxil_text.splitlines()
    names = hlsl_anchor_names(hlsl_chunk)
    scored = []
    for i, line in enumerate(dxil_lines):
        score = score_dxil_line(line, names)
        if score > 0:
            scored.append((score, i))

    selected = set()
    for _, idx in sorted(scored, reverse=True)[:max_lines]:
        for j in range(max(0, idx - 2), min(len(dxil_lines), idx + 3)):
            selected.add(j)

    if not selected:
        # Fallback to a proportional window. It is rough, but gives the model
        # local context instead of the whole file.
        return line_window(dxil_text, dxil_first_line, dxil_first_line, max_lines)

    ordered = sorted(selected)
    if len(ordered) > max_lines:
        ordered = ordered[:max_lines]

    rendered = []
    prev = None
    first_selected_line = dxil_first_line + ordered[0]
    for idx in ordered:
        if prev is not None and idx > prev + 1:
            rendered.append("... <gap> ...")
        rendered.append(dxil_lines[idx])
        prev = idx
    return "\n".join(rendered), first_selected_line


def merge_chunk_results(results, metadata):
    merged = {
        "summary": "Chunked DXIL/HLSL mapping. Inspect chunk_results for per-window failures or lower-confidence regions.",
        "confidence": "medium",
        "mappings": [],
        "unmatched_hlsl_lines": [],
        "unmatched_dxil_lines": [],
        "chunk_results": results,
        "metadata": metadata,
    }
    for chunk in results:
        result = chunk.get("result") or {}
        merged["mappings"].extend(result.get("mappings") or [])
        merged["unmatched_hlsl_lines"].extend(result.get("unmatched_hlsl_lines") or [])
        merged["unmatched_dxil_lines"].extend(result.get("unmatched_dxil_lines") or [])
    return merged


def _normalize_range(value):
    if not isinstance(value, list) or not value:
        return None
    try:
        if len(value) == 1:
            a = b = int(value[0])
        else:
            a, b = int(value[0]), int(value[-1])
        if b < a:
            a, b = b, a
        return a, b
    except Exception:
        return None


def _merge_ranges(ranges):
    cleaned = sorted(r for r in ranges if r is not None)
    merged = []
    for a, b in cleaned:
        if not merged or a > merged[-1][1] + 1:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return merged


def _invert_ranges(ranges, start, end):
    gaps = []
    cur = start
    for a, b in ranges:
        if a > cur:
            gaps.append([cur, a - 1])
        cur = max(cur, b + 1)
    if cur <= end:
        gaps.append([cur, end])
    return gaps


def add_coverage_report(result, hlsl_text, dxil_text):
    hlsl_line_count = len(hlsl_text.splitlines())
    dxil_line_count = len(dxil_text.splitlines())
    hlsl_ranges = []
    dxil_ranges = []
    for m in result.get("mappings") or []:
        hlsl_ranges.append(_normalize_range(m.get("hlsl_lines")))
        dxil_ranges.append(_normalize_range(m.get("dxil_lines")))
    covered_hlsl = _merge_ranges(hlsl_ranges)
    covered_dxil = _merge_ranges(dxil_ranges)
    unmatched_hlsl = _merge_ranges([_normalize_range(r) for r in result.get("unmatched_hlsl_lines") or []])
    unmatched_dxil = _merge_ranges([_normalize_range(r) for r in result.get("unmatched_dxil_lines") or []])
    result["coverage_report"] = {
        "hlsl_line_count": hlsl_line_count,
        "dxil_line_count": dxil_line_count,
        "covered_hlsl_line_ranges": covered_hlsl,
        "covered_dxil_line_ranges": covered_dxil,
        "unmatched_hlsl_line_ranges": unmatched_hlsl,
        "unmatched_dxil_line_ranges": unmatched_dxil,
        "uncovered_hlsl_line_ranges": _invert_ranges(_merge_ranges([tuple(r) for r in covered_hlsl + unmatched_hlsl]), 1, hlsl_line_count),
        "uncovered_dxil_line_ranges": _invert_ranges(_merge_ranges([tuple(r) for r in covered_dxil + unmatched_dxil]), 1, dxil_line_count),
    }
    return result


def run_chunked(args, dxil_text, dxil_first_line, hlsl_text, extracted):
    hlsl_lines = hlsl_text.splitlines()
    chunk_results = []
    start = 1
    chunk_id = 0
    while start <= len(hlsl_lines):
        chunk_id += 1
        hlsl_chunk, hlsl_first = line_window(hlsl_text, 1, start, args.chunk_hlsl_lines)
        dxil_chunk, dxil_chunk_first = related_dxil_for_hlsl_chunk(
            dxil_text, dxil_first_line, hlsl_chunk, args.chunk_dxil_lines)
        prompt, warnings = build_prompt(dxil_chunk, dxil_chunk_first, hlsl_chunk, args.max_chars_per_file)
        entry = {
            "chunk_id": chunk_id,
            "hlsl_lines": [hlsl_first, hlsl_first + len(hlsl_chunk.splitlines()) - 1],
            "dxil_context_first_line": dxil_chunk_first,
            "warnings": warnings,
        }
        try:
            content = call_chat_completions(args.base_url, args.api_key, args.model,
                                            SYSTEM_PROMPT, prompt, args.temperature,
                                            args.timeout, args.retries)
            entry["result"] = parse_json_response(content)
            print("chunk %d ok: HLSL %d-%d" % (chunk_id, entry["hlsl_lines"][0], entry["hlsl_lines"][1]))
        except Exception as exc:
            entry["error"] = str(exc)
            print("chunk %d failed: %s" % (chunk_id, exc))
        chunk_results.append(entry)
        start += max(1, args.chunk_hlsl_lines - args.chunk_overlap)

    metadata = {
        "model": args.model,
        "base_url": args.base_url,
        "dxil_ir": os.path.abspath(args.dxil_ir),
        "hlsl": os.path.abspath(args.hlsl),
        "renderdoc_disassembly_extracted": extracted,
        "dxil_first_line": dxil_first_line,
        "chunk_hlsl_lines": args.chunk_hlsl_lines,
        "chunk_dxil_lines": args.chunk_dxil_lines,
        "chunk_overlap": args.chunk_overlap,
    }
    return merge_chunk_results(chunk_results, metadata)


def endpoint_from_base_url(base_url):
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"


def call_chat_completions(base_url, api_key, model, system_prompt, user_prompt,
                          temperature, timeout, retries):
    if not api_key:
        raise ValueError("missing API key; pass --api-key")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    body = json.dumps(payload).encode("utf-8")
    endpoint = endpoint_from_base_url(base_url)
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(max(1, retries + 1)):
        req = urlrequest.Request(endpoint, data=body, headers=headers)
        try:
            resp = urlrequest.urlopen(req, timeout=timeout)
            raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("LLM request failed: %s" % last_error)


def parse_json_response(text):
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start:end + 1])
        raise


def write_json(path, obj):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Map decompiled HLSL lines to DXIL IR lines using an LLM")
    parser.add_argument("--dxil-ir", required=True, help="Textual DXIL IR, LLVM .ll, or RenderDoc debug dump")
    parser.add_argument("--hlsl", required=True, help="Decompiled HLSL file")
    parser.add_argument("--out", default="dxil_hlsl_map.json", help="Output JSON path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-chars-per-file", type=int, default=120000)
    parser.add_argument("--no-auto-extract", action="store_true", help="Do not auto-extract RenderDoc '--- Disassembly ---' section")
    parser.add_argument("--print-prompt", action="store_true", help="Print the prompt and do not call the API")
    parser.add_argument("--direct-full", action="store_true", help="Send both full line-numbered files directly without clipping or chunking")
    parser.add_argument("--chunked", action="store_true", help="Analyze HLSL in overlapping chunks and merge the JSON results")
    parser.add_argument("--chunk-hlsl-lines", type=int, default=80, help="HLSL lines per chunk when --chunked is set")
    parser.add_argument("--chunk-dxil-lines", type=int, default=220, help="Related DXIL context lines per chunk when --chunked is set")
    parser.add_argument("--chunk-overlap", type=int, default=10, help="Overlapping HLSL lines between chunks")
    args = parser.parse_args(argv)

    dxil_raw = read_text(args.dxil_ir)
    if args.no_auto_extract:
        dxil_text, dxil_first_line, extracted = dxil_raw, 1, False
    else:
        dxil_text, dxil_first_line, extracted = extract_renderdoc_disassembly(dxil_raw)
    hlsl_text = read_text(args.hlsl)

    if args.direct_full:
        prompt = build_direct_prompt(dxil_text, dxil_first_line, hlsl_text)
        warnings = []
    else:
        prompt, warnings = build_prompt(dxil_text, dxil_first_line, hlsl_text, args.max_chars_per_file)
    if args.print_prompt:
        print(SYSTEM_PROMPT)
        print("\n--- USER PROMPT ---\n")
        print(prompt)
        return 0

    if args.chunked:
        result = run_chunked(args, dxil_text, dxil_first_line, hlsl_text, extracted)
        write_json(args.out, result)
        print("Wrote %s" % args.out)
        return 0

    content = call_chat_completions(args.base_url, args.api_key, args.model,
                                    SYSTEM_PROMPT, prompt, args.temperature,
                                    args.timeout, args.retries)
    metadata = {
        "model": args.model,
        "base_url": args.base_url,
        "dxil_ir": os.path.abspath(args.dxil_ir),
        "hlsl": os.path.abspath(args.hlsl),
        "renderdoc_disassembly_extracted": extracted,
        "dxil_first_line": dxil_first_line,
        "warnings": warnings,
    }

    try:
        result = parse_json_response(content)
        result["metadata"] = metadata
        add_coverage_report(result, hlsl_text, dxil_text)
        write_json(args.out, result)
        print("Wrote %s" % args.out)
        return 0
    except Exception as exc:
        fallback = {
            "ok": False,
            "error": "model response was not valid JSON: %s" % exc,
            "raw_response": content,
            "metadata": metadata,
        }
        write_json(args.out, fallback)
        print("Wrote raw response to %s" % args.out)
        return 2


if __name__ == "__main__":
    sys.exit(main())
