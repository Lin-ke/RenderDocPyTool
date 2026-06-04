# RenderDocPyTool

A small Python framework around the [RenderDoc](https://renderdoc.org/) replay
controller, plus example scripts that show how to compose its primitives into
practical capture-analysis tools.

The framework lives in a single module, [`rdoc_tool.py`](rdoc_tool.py). It
exposes generic, composable APIs designed so that the *consumer* writes very
little code and never has to special-case the slow paths.

## Why this exists

The naive way to scan a capture is

```python
for action in flatten_actions(controller.GetRootActions()):
    controller.SetFrameEvent(action.eventId, True)
    pipe = controller.GetPipelineState()
    ...
```

`SetFrameEvent` re-walks the GPU command stream and on a 4 GB capture costs
~0.8 s per draw. With thousands of draws and tens of thousands of
non-draw events (markers, push-groups, clears, copies, indirect headers,
pass boundaries) you easily blow past an hour per file.

`rdoc_tool` ships three orthogonal primitives that, combined, fix this:

1. **`is_draw(action)` / `iter_passes(action_filter=is_draw)`** — drop the
   non-draw events *before* paying for `SetFrameEvent`.
2. **`get_pass(eid_or_action)`** — accept an `Action` directly so iterators
   skip the `find_action` recursion.
3. **`find_events_using(resources, usage_filter=...)`** — a millisecond-scale
   reverse index built on top of `controller.GetUsage`. Most "which draws
   read these resources?" questions can be answered without any replay at
   all; whatever survives that filter can then be verified with a much
   smaller `iter_passes` sweep.

On a 4 GB Mirage capture, `examples/search_texture.py` went from ~60 minutes
(naive sweep) to ~4 minutes (filtered sweep) to ~2 minutes (reverse index +
verification of just the survivors).

## Layout

```
.
├── rdoc_tool.py                  # the framework
├── rdc_tool.json.example         # config template (copy to rdc_tool.json)
└── examples/
    └── search_texture.py         # find passes binding multiple BCn textures
```

## Setup

1. Build / install RenderDoc with the Python bindings. The standalone build
   ships them under `<renderdoc>/x64/Development/pymodules`.
2. Make Python 3.6+ available (the bindings ship as a CPython extension).
3. Clone this repo, then copy the config template:

   ```powershell
   git clone git@github.com:Lin-ke/RenderDocPyTool.git
   cd RenderDocPyTool
   copy rdc_tool.json.example rdc_tool.json
   ```
4. Edit `rdc_tool.json` to point at your captures.

## Running an example

`PATH` must include both the RenderDoc binaries and the `pymodules` directory;
`PYTHONPATH` must include `pymodules`. From PowerShell:

```powershell
$env:PATH = "<renderdoc>\x64\Development;<renderdoc>\x64\Development\pymodules;" + $env:PATH
$env:PYTHONPATH = "<renderdoc>\x64\Development\pymodules"
python examples\search_texture.py
```

The example reads `rdc_tool.json`, iterates every entry in `captures`, and
writes one report per capture next to the input `.rdc` (or in `out_dir`
when set).

## Configuration

`rdc_tool.json` stores capture paths and tool paths. Search parameters are
passed by the caller. The loader (`rdoc_tool.load_config`) searches in this order:

1. an explicit path passed to `load_config(path=...)`;
2. `rdc_tool.json` in the current working directory;
3. `rdc_tool.json` next to `rdoc_tool.py`.

For `search_texture.py`, config only supplies `captures`:

| Key                          | Default       | Meaning                                                          |
| ---------------------------- | ------------- | ---------------------------------------------------------------- |
| `captures`                   | `[]`          | List of `.rdc` paths to scan                                     |
Pass scan options directly:

```python
search_texture(format="BC1_UNorm", min_textures=2, limit=0,
               out_dir=None, out_suffix="passes",
               scan_execute_indirect=True, descriptor_scan_limit=0,
               stage="pixel")
```

## API tour

### Discovery (no replay required)

```python
tool.filter_textures(predicate)            # cached GetTextures snapshot
tool.filter_resources(predicate)           # textures + buffers
tool.get_usage(resource)                   # ReplayController.GetUsage wrapper
tool.find_events_using(resources, usage_filter=is_shader_read_usage)
                                           # -> {eid: {ResourceId, ...}}
```

`is_shader_read_usage` collapses D3D12's `All_Resource` bucket together with
the per-stage `*_Resource` enums so that "any shader stage read of this
resource" is one predicate.

### Iteration (one replay per yielded pass)

```python
tool.iter_passes(actions=None, action_filter=is_draw, tag=MIN_ACCESS)
tool.get_pass(eid_or_action, tag=MIN_ACCESS)
```

`iter_passes` accepts a pre-flattened `actions` list (handy when you want a
total for a progress bar or want to scope the sweep) and a filter that runs
*before* `SetFrameEvent`. The default filter, `is_draw`, keeps only graphics
drawcalls and mesh dispatches.

### Per-pass access (lazy)

```python
pass_.getPixelTextures()   # only the PS stage bindings
pass_.textures             # union across stages, cached after first access
pass_.outputs              # RTV/DSV
pass_.pixelShader          # entry / encoding / disassembly
```

Bindings load on demand. A `Pass` constructed with the default `MIN_ACCESS`
tag does no I/O until you ask it for something.

## A worked example (`examples/search_texture.py`)

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

Step 1 is a snapshot lookup, step 2 is two `GetUsage` calls and a dict, and
step 3 only replays the events that survived steps 1+2.

## License

Pick whatever fits your project; this repo ships without one.
