# RenderDocPyTool

A Python toolkit for analyzing and debugging [RenderDoc](https://renderdoc.org/) captures. It wraps the RenderDoc replay controller with a higher-level API and provides practical scripts for texture search, pass export, shader debugging, and HLSL decompilation.

**Design principle**: All configuration is passed explicitly via config dicts and function arguments. No environment variables, no global state, no `sys.path` mutation.

## Why this exists

The naive way to scan a capture is:

```python
for action in flatten_actions(controller.GetRootActions()):
    controller.SetFrameEvent(action.eventId, True)
    pipe = controller.GetPipelineState()
    ...
```

`SetFrameEvent` re-walks the GPU command stream and on a 4 GB capture costs ~0.8 s per draw. With thousands of draws and tens of thousands of non-draw events (markers, push-groups, clears, copies, indirect headers, pass boundaries) you easily blow past an hour per file.

`rdoc_interface.py` ships three orthogonal primitives that, combined, fix this:

1. **`is_draw(action)` / `iter_passes(action_filter=is_draw)`** — drop the non-draw events *before* paying for `SetFrameEvent`.
2. **`get_pass(eid_or_action)`** — accept an `Action` directly so iterators skip the `find_action` recursion.
3. **`find_events_using(resources, usage_filter=...)`** — a millisecond-scale reverse index built on top of `controller.GetUsage`. Most "which draws read these resources?" questions can be answered without any replay at all; whatever survives that filter can then be verified with a much smaller `iter_passes` sweep.

On a 4 GB Mirage capture, `example/search_texture.py` went from ~60 minutes (naive sweep) to ~4 minutes (filtered sweep) to ~2 minutes (reverse index + verification of just the survivors).

## Layout

```
.
├── rdoc_interface.py             # Core framework: replay abstraction, Pass objects, discovery helpers
├── hlsl_decompiler_tool.py       # DXBC/DXIL/SPIR-V -> HLSL decompiler wrapper
├── llm_dxil_hlsl_mapper.py       # LLM-based DXIL IR <-> HLSL line mapping
├── export_texture.py             # Export textures from search reports
├── export_pass_debug.py          # Dump full shader debug state for a draw call
├── example/
│   └── search_texture.py         # Find passes binding multiple BCn textures
├── config.py                     # (reserved for future config utilities)
├── __init__.py
└── rdc_tool.json.example         # Config template (copy to rdc_tool.json)
```

## Setup

1. Build / install RenderDoc with the Python bindings. The standalone build ships them under `<renderdoc>/x64/Development/pymodules`.
2. Make Python 3.6+ available (the bindings ship as a CPython extension).
3. Clone this repo, then copy the config template:

   ```powershell
   git clone git@github.com:Lin-ke/RenderDocPyTool.git
   cd RenderDocPyTool
   copy rdc_tool.json.example rdc_tool.json
   ```
4. Edit `rdc_tool.json` to point at your captures and RenderDoc paths.

## Configuration

`rdc_tool.json` is the single source of truth. All scripts read from it; nothing reads from environment variables or global state.

```json
{
  "captures": [
    "path/to/your/capture1.rdc",
    "path/to/your/capture2.rdc"
  ],
  "renderdoc": {
    "development_dir": "path/to/renderdoc/x64/Development",
    "pymodules_dir": "path/to/renderdoc/x64/Development/pymodules"
  }
}
```

The loader (`rdoc_interface.load_config`) searches in this order:

1. An explicit path passed to `load_config(path=...)` or `--config`;
2. `rdc_tool.json` in the current working directory;
3. `rdc_tool.json` next to `rdoc_interface.py`.

## Running examples

All parameters are passed via config or function arguments. No `PYTHONPATH` or `PATH` manipulation is required.

### Texture Search (`example/search_texture.py`)

```powershell
python example/search_texture.py --config rdc_tool.json
```

Or programmatically:

```python
from RenderDocPyTool.example.search_texture import search_texture

search_texture(
    format="BC1_UNorm",
    min_textures=4,
    stage="pixel",
    config_path="rdc_tool.json"
)
```

### Texture Export (`export_texture.py`)

```python
from RenderDocPyTool.export_texture import export_texture

export_texture(
    pass_report="capture.BC1_passes.txt",
    out_dir="exported_textures",
    file_type="dds",
    verify_replay=True,
    config_path="rdc_tool.json"
)
```

### Pass Debug Dump (`export_pass_debug.py`)

Can be run standalone or via `qrenderdoc.exe --python`.

**Runtime parameters** (passed as function arguments only):

```python
from RenderDocPyTool.export_pass_debug import export_pass_debug

export_pass_debug(
    rdc_path=r"E:\mirage\mirage1.rdc",
    eid=8934,
    stage="pixel",
    pixel_xy=(640, 360),
    max_steps=1000
)
```

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `rdc_path` | — | Path to `.rdc` file (required) |
| `eid` | — | Event ID to debug (required) |
| `stage` | `pixel` | Shader stage |
| `pixel_xy` | viewport center | `(x, y)` or `"X,Y"` pixel coordinates |
| `group` | `(0, 0, 0)` | Compute group `(x, y, z)` |
| `thread` | `(0, 0, 0)` | Compute thread `(x, y, z)` |
| `out_path` | `<rdc>.eid<eid>.debug.txt` | Output text file |
| `out_dir` | `<out>.<stage>` | Output folder |
| `max_steps` | `0` | Step cap (0 = unlimited) |

**LLM config** (read from `llm` section in `rdc_tool.json`):

```json
{
  "llm": {
    "llm_map_shader": true,
    "llm_model": "deepseek-v4-pro",
    "llm_api_key": "your-key"
  }
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `llm_map_shader` | `false` | Enable LLM DXIL-HLSL mapping |
| `llm_mapper_script` | auto | Path to mapper script |
| `llm_model` | `""` | LLM model name |
| `llm_base_url` | `""` | API base URL |
| `llm_api_key` | `""` | API key |
| `llm_timeout` | `900` | Request timeout |

### HLSL Decompilation (`hlsl_decompiler_tool.py`)

```python
from RenderDocPyTool.hlsl_decompiler_tool import decompile_shader

result = decompile_shader(
    input_path="shader.dxbc",
    encoding_name="DXBC",
    output_path="shader.hlsl",
    tool_hint="D:/HLSL-Decompiler",
    tool_paths={
        "dxbc2dxil": "D:/tools/dxbc2dxil.exe",
        "dxil_spirv": "D:/tools/dxil-spirv.exe",
        "spirv_cross": "D:/tools/spirv-cross.exe"
    }
)
```

Tool chain: `DXBC -> dxbc2dxil -> dxil-spirv -> spirv-cross -> HLSL`

### DXIL-HLSL Mapping (`llm_dxil_hlsl_mapper.py`)

```python
from RenderDocPyTool.llm_dxil_hlsl_mapper import main

main([
    "--dxil-ir", "debug_dump.txt",
    "--hlsl", "pixel_shader.hlsl",
    "--out", "dxil_hlsl_map.json",
    "--api-key", "your-api-key",
    "--model", "deepseek-v4-pro",
    "--base-url", "https://api.deepseek.com"
])
```

Supports chunked analysis for large shaders and coverage reporting.

## API Tour

### Discovery (no replay required)

```python
tool.filter_textures(predicate)            # cached GetTextures snapshot
tool.filter_resources(predicate)           # textures + buffers
tool.get_usage(resource)                   # ReplayController.GetUsage wrapper
tool.find_events_using(resources, usage_filter=is_shader_read_usage)
                                           # -> {eid: {ResourceId, ...}}
```

`is_shader_read_usage` collapses D3D12's `All_Resource` bucket together with the per-stage `*_Resource` enums so that "any shader stage read of this resource" is one predicate.

### Iteration (one replay per yielded pass)

```python
tool.iter_passes(actions=None, action_filter=is_draw, tag=MIN_ACCESS)
tool.get_pass(eid_or_action, tag=MIN_ACCESS)
```

`iter_passes` accepts a pre-flattened `actions` list (handy when you want a total for a progress bar or want to scope the sweep) and a filter that runs *before* `SetFrameEvent`. The default filter, `is_draw`, keeps only graphics drawcalls and mesh dispatches.

### Per-pass access (lazy)

```python
pass_.getPixelTextures()   # only the PS stage bindings
pass_.textures             # union across stages, cached after first access
pass_.outputs              # RTV/DSV
pass_.pixelShader          # entry / encoding / disassembly
```

Bindings load on demand. A `Pass` constructed with the default `MIN_ACCESS` tag does no I/O until you ask it for something.

## Worked Example (`example/search_texture.py`)

The complete search loop is just:

```python
matched_texs = tool.filter_textures(
    lambda t: needle in format_name(t.format).lower())

by_event = tool.find_events_using(matched_texs,
                                  usage_filter=is_shader_read_usage)
candidates = {eid for eid, rids in by_event.items()
              if len(rids) >= min_textures}

cand_actions = [a for a in flatten_actions(tool.controller.GetRootActions())
                if a.eventId in candidates and is_draw(a)]

for pass_ in tool.iter_passes(cand_actions, action_filter=None):
    matches = [t for t in pass_.getPixelTextures()
               if needle in t.format.lower()]
    if len(matches) >= min_textures:
        results.append((pass_.action, matches))
```

Step 1 is a snapshot lookup, step 2 is two `GetUsage` calls and a dict, and step 3 only replays the events that survived steps 1+2.

## License

Pick whatever fits your project; this repo ships without one.
