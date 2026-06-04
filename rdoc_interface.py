# -*- coding: utf-8 -*-
"""Small RenderDoc replay abstraction used by the local debug scripts."""

from __future__ import print_function

import json
import os

import renderdoc as rd


CONFIG_FILENAME = "rdc_tool.json"


def load_config(path=None):
    """Load the project JSON config and return ``(config_dict, abs_path)``.

    Search order:
      1. ``path`` argument, if given.
      2. ``rdc_tool.json`` in the current working directory.
      3. ``rdc_tool.json`` next to this module.

    Raises :class:`FileNotFoundError` if no config is found. Per-script
    sections live under their own keys (e.g. ``search_texture``); the
    top-level ``captures`` list applies to every consumer.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if path:
        candidates.append(os.path.abspath(path))
    candidates.append(os.path.join(os.getcwd(), CONFIG_FILENAME))
    candidates.append(os.path.join(here, CONFIG_FILENAME))
    for c in candidates:
        if c and os.path.isfile(c):
            with open(c, "r", encoding="utf-8") as f:
                return json.load(f), os.path.abspath(c)
    raise FileNotFoundError(
        "%s not found. Looked in: %s" % (CONFIG_FILENAME, candidates))


def initialise_replay():
    """Initialise RenderDoc replay through the local wrapper module."""
    return rd.InitialiseReplay(rd.GlobalEnvironment(), [])


def shutdown_replay():
    """Shutdown RenderDoc replay through the local wrapper module."""
    return rd.ShutdownReplay()


FULL_ACCESS = "FULL_ACCESS"
SHADER_ACCESS = "SHADER_ACCESS"
BINDING_ACCESS = "BINDING_ACCESS"
PIXEL_TEXTURE_ACCESS = "PIXEL_TEXTURE_ACCESS"
MIN_ACCESS = "MIN_ACCESS"


def is_success(res):
    if res is None:
        return False
    if hasattr(res, "code"):
        try:
            return res.code == rd.ResultCode.Succeeded
        except Exception:
            pass
    try:
        return res == rd.ResultCode.Succeeded
    except Exception:
        return False


def result_msg(res):
    try:
        return res.Message()
    except Exception:
        return str(res)


def flatten_actions(actions, out=None):
    out = [] if out is None else out
    for action in actions:
        out.append(action)
        flatten_actions(action.children, out)
    return out


def find_action(actions, eid):
    for action in actions:
        if action.eventId == eid:
            return action
        found = find_action(action.children, eid)
        if found is not None:
            return found
    return None


def is_draw(action):
    """True if the action is a real drawcall (graphics or mesh dispatch).

    Used to skip markers / push-groups / clears / copies / non-draw boundaries
    before paying for ``SetFrameEvent``, which is the dominant cost when
    sweeping a full capture.
    """
    flags = getattr(action, "flags", None)
    if not flags:
        return False
    try:
        mask = rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch
    except Exception:
        try:
            mask = rd.ActionFlags.Drawcall
        except Exception:
            return False
    try:
        return bool(flags & mask)
    except Exception:
        return False


def is_dispatch(action):
    """True if the action is a compute dispatch."""
    flags = getattr(action, "flags", None)
    if not flags:
        return False
    try:
        return bool(flags & rd.ActionFlags.Dispatch)
    except Exception:
        return False


def _build_shader_read_set():
    names = ("All_Resource", "PS_Resource", "VS_Resource", "HS_Resource",
             "DS_Resource", "GS_Resource", "CS_Resource", "MS_Resource",
             "TS_Resource")
    out = set()
    for name in names:
        val = getattr(rd.ResourceUsage, name, None)
        if val is not None:
            out.add(val)
    return out


_SHADER_READ_USAGES = None


def is_shader_read_usage(usage):
    """True if ``usage.usage`` represents a shader-stage read.

    Covers per-stage SRV reads (``PS_Resource`` etc.) and the
    ``All_Resource`` bucket that D3D12 captures use when a descriptor table
    is visible to all stages. Excludes RTV/DSV writes, copies, barriers.
    """
    global _SHADER_READ_USAGES
    if _SHADER_READ_USAGES is None:
        _SHADER_READ_USAGES = _build_shader_read_set()
    try:
        return usage.usage in _SHADER_READ_USAGES
    except Exception:
        return False


def action_name(action, controller):
    if hasattr(action, "GetName"):
        try:
            return action.GetName(controller.GetStructuredFile())
        except Exception:
            pass
    return getattr(action, "name", "") or "<unnamed>"


def null_resource_id():
    try:
        return rd.ResourceId.Null()
    except Exception:
        return None


def is_null_handle(handle):
    if handle is None:
        return True
    null_id = null_resource_id()
    if null_id is not None:
        try:
            return handle == null_id
        except Exception:
            pass
    return str(handle) in ("ResourceId::0", "0")


def format_name(fmt):
    try:
        return fmt.Name()
    except Exception:
        return str(fmt)


def file_type_from_name(name):
    """Resolve a file type name/extension to ``rd.FileType``."""
    if name is None:
        return rd.FileType.DDS
    if hasattr(name, "name"):
        return name
    key = str(name).strip().lower()
    if key.startswith("."):
        key = key[1:]
    mapping = {
        "dds": rd.FileType.DDS,
        "png": rd.FileType.PNG,
        "jpg": rd.FileType.JPG,
        "jpeg": rd.FileType.JPG,
        "bmp": rd.FileType.BMP,
        "tga": rd.FileType.TGA,
        "hdr": rd.FileType.HDR,
        "exr": rd.FileType.EXR,
        "raw": rd.FileType.Raw,
    }
    if key in mapping:
        return mapping[key]
    raise ValueError("unsupported texture file type: %s" % name)


def texture_file_extension(file_type):
    """Return the conventional extension for a RenderDoc texture file type."""
    ft = file_type_from_name(file_type)
    mapping = {
        rd.FileType.DDS: "dds",
        rd.FileType.PNG: "png",
        rd.FileType.JPG: "jpg",
        rd.FileType.BMP: "bmp",
        rd.FileType.TGA: "tga",
        rd.FileType.HDR: "hdr",
        rd.FileType.EXR: "exr",
        rd.FileType.Raw: "raw",
    }
    return mapping.get(ft, str(ft).lower().replace("filetype.", ""))


def is_texture_descriptor(desc):
    if desc is None:
        return False
    try:
        if desc.textureType == rd.TextureType.Buffer:
            return False
    except Exception:
        pass
    try:
        return desc.type in (rd.DescriptorType.Image, rd.DescriptorType.ImageSampler,
                             rd.DescriptorType.ReadWriteImage)
    except Exception:
        return False


def is_buffer_descriptor(desc):
    if desc is None:
        return False
    try:
        if desc.textureType == rd.TextureType.Buffer:
            return True
    except Exception:
        pass
    try:
        return desc.type in (rd.DescriptorType.ConstantBuffer, rd.DescriptorType.Buffer,
                             rd.DescriptorType.TypedBuffer, rd.DescriptorType.ReadWriteBuffer,
                             rd.DescriptorType.ReadWriteTypedBuffer)
    except Exception:
        return False


class ResourceState(object):
    def __init__(self, handle, name="", kind="resource", descriptor=None, texture=None, resource=None):
        self.handle = handle
        self.name = name or str(handle)
        self.kind = kind
        self.descriptor = descriptor
        self.texture = texture
        self.resource = resource

    @property
    def format(self):
        if self.texture is not None:
            return format_name(self.texture.format)
        if self.descriptor is not None:
            return format_name(self.descriptor.format)
        return ""

    @property
    def width(self):
        return getattr(self.texture, "width", 0)

    @property
    def height(self):
        return getattr(self.texture, "height", 0)

    @property
    def mips(self):
        return getattr(self.texture, "mips", 0)

    @property
    def arraysize(self):
        return getattr(self.texture, "arraysize", 0)

    def label(self):
        parts = [self.name]
        if not is_null_handle(self.handle):
            parts.append("(%s)" % self.handle)
        if self.format:
            parts.append(self.format)
        if self.texture is not None:
            parts.append("%dx%d" % (self.width, self.height))
        if self.descriptor is not None:
            try:
                dtype = self.descriptor.type.name
                if dtype and dtype != "Unknown":
                    parts.append("type=%s" % dtype)
            except Exception:
                pass
            try:
                ttype = self.descriptor.textureType.name
                if ttype and ttype != "Unknown":
                    parts.append("view=%s" % ttype)
            except Exception:
                pass
        return "  ".join(parts)


class Binding(object):
    def __init__(self, category, stage, index, array_element, handle, descriptor=None, sampler=None,
                 shader_name="", access=None):
        self.category = category
        self.stage = stage
        self.index = index
        self.array_element = array_element
        self.handle = handle
        self.descriptor = descriptor
        self.sampler = sampler
        self.shader_name = shader_name
        self.access = access


class LazyResourceCollection(object):
    def __init__(self, pass_, kind, stage=None, include_outputs=True):
        self.pass_ = pass_
        self.kind = kind
        self.stage = stage
        self.include_outputs = include_outputs
        self._loaded = False
        self._items = []

    def _load(self):
        if self._loaded:
            return
        if self.kind == "texture":
            self._items = self.pass_._load_textures(self.stage, self.include_outputs)
        elif self.kind == "buffer":
            self._items = self.pass_._load_buffers(self.stage)
        elif self.kind == "shader":
            self._items = self.pass_._load_shaders(self.stage)
        elif self.kind == "sampler":
            self._items = self.pass_._load_samplers(self.stage)
        elif self.kind == "input":
            self._items = self.pass_._load_inputs(self.stage)
        elif self.kind == "output":
            self._items = self.pass_._load_outputs(self.stage)
        else:
            self._items = []
        self._loaded = True

    def __iter__(self):
        self._load()
        return iter(self._items)

    def __len__(self):
        self._load()
        return len(self._items)

    def __getitem__(self, idx):
        self._load()
        return self._items[idx]

    def list(self):
        self._load()
        return list(self._items)

class Shader(object):
    def __init__(self, pass_, stage):
        self.pass_ = pass_
        self.tool = pass_.tool
        self.controller = pass_.controller
        self.pipe = pass_.pipe
        self.stage = stage
        self.handle = self.pipe.GetShader(stage)
        self.entry = self.pipe.GetShaderEntryPoint(stage)
        self.reflection = self.pipe.GetShaderReflection(stage)
        self.name = self.tool.getResource(self.handle).name
        if self.reflection is not None:
            self.name = self.name or getattr(self.reflection, "entryPoint", "") or str(self.handle)
            try:
                self.entry = self.reflection.entryPoint or self.entry
            except Exception:
                pass

    @property
    def encoding(self):
        if self.reflection is None:
            return ""
        try:
            return self.reflection.encoding.name
        except Exception:
            return str(getattr(self.reflection, "encoding", ""))

    @property
    def raw_bytes(self):
        if self.reflection is None:
            return b""
        raw = getattr(self.reflection, "rawBytes", None)
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

    @property
    def debug_source_files(self):
        if self.reflection is None:
            return []
        dbg = getattr(self.reflection, "debugInfo", None)
        files = getattr(dbg, "files", None) if dbg is not None else None
        if not files:
            return []
        return [(getattr(f, "filename", "source_%d.hlsl" % i), getattr(f, "contents", ""))
                for i, f in enumerate(files)]

    def disassemble(self, target=""):
        if self.reflection is None:
            return ""
        if not target:
            targets = []
            try:
                targets = list(self.controller.GetDisassemblyTargets(False))
            except Exception:
                try:
                    targets = list(self.controller.GetDisassemblyTargets(True))
                except Exception:
                    targets = []
            target = targets[0] if targets else ""
        pipeline_id = _pipeline_object(self.pipe, self.stage)
        return self.controller.DisassembleShader(pipeline_id, self.reflection, target)

    def label(self):
        return "%s (%s) entry=%s encoding=%s" % (self.name, self.handle, self.entry, self.encoding)


def _pipeline_object(pipe, stage):
    if stage == rd.ShaderStage.Compute:
        try:
            return pipe.GetComputePipelineObject()
        except Exception:
            pass
    return pipe.GetGraphicsPipelineObject()


class Pass(object):
    def __init__(self, tool, action, pipe, tag=MIN_ACCESS):
        self.tool = tool
        self.controller = tool.controller
        self.action = action
        self.eventId = action.eventId
        self.name = action_name(action, self.controller)
        self.pipe = pipe
        self.tag = tag
        self._inputs = []
        self._outputs = []
        self._textures = []
        self._buffers = []
        self._samplers = []
        self.shaders = {}
        self._bindings_loaded = False
        self._outputs_loaded = False
        self._stage_bindings_loaded = set()
        self._stage_shaders_loaded = set()
        if tag == FULL_ACCESS:
            self.load_all()
        elif tag == SHADER_ACCESS:
            self.load_shaders()
        elif tag == BINDING_ACCESS:
            self.load_bindings()
        elif tag == PIXEL_TEXTURE_ACCESS:
            self.load_stage_bindings(rd.ShaderStage.Pixel)

    @property
    def inputs(self):
        self.load_bindings()
        return self._inputs

    @property
    def outputs(self):
        self.load_bindings()
        return self._outputs

    @property
    def textures(self):
        self.load_bindings()
        return self._textures

    @property
    def buffers(self):
        self.load_bindings()
        return self._buffers

    @property
    def samplers(self):
        self.load_bindings()
        return self._samplers

    def getTexture(self, handle=None, stage=None, include_outputs=True):
        """Return a texture state by handle, or all bound texture states.

        With ``handle`` omitted this is lazy: the required binding lists are
        populated on first access, then cached on the Pass.
        """
        if handle is None:
            return LazyResourceCollection(self, "texture", stage, include_outputs)
        desc = None
        if isinstance(handle, Binding):
            desc = handle.descriptor
            handle = handle.handle
        state = self.tool.getTexture(handle, self)
        if state is not None and desc is not None:
            state.descriptor = desc
        return state

    def getBuffer(self, handle=None, stage=None):
        if handle is None:
            return LazyResourceCollection(self, "buffer", stage)
        desc = None
        if isinstance(handle, Binding):
            desc = handle.descriptor
            handle = handle.handle
        state = self.tool.getBuffer(handle, self)
        if state is not None and desc is not None:
            state.descriptor = desc
        return state

    def getResource(self, handle):
        if handle is None:
            return LazyResourceCollection(self, "input")
        return self.tool.getResource(handle, self)

    def getShader(self, stage=None):
        if stage is None:
            return LazyResourceCollection(self, "shader")
        shader = self.shaders.get(stage)
        if shader is None:
            shader = Shader(self, stage)
            if not is_null_handle(shader.handle):
                self.shaders[stage] = shader
                self._stage_shaders_loaded.add(stage)
            else:
                shader = None
        return shader

    def getSampler(self, stage=None):
        return LazyResourceCollection(self, "sampler", stage)

    def _add_descriptor_binding(self, category, used):
        desc = getattr(used, "descriptor", None)
        access = getattr(used, "access", None)
        sampler = getattr(used, "sampler", None)
        handle = getattr(desc, "resource", None)
        idx = getattr(access, "index", len(self._inputs))
        arr = getattr(access, "arrayElement", 0)
        stage = getattr(access, "stage", None)
        binding = Binding(category, stage, idx, arr, handle, desc, sampler, "", access)

        if category in ("srv", "cbv"):
            self._inputs.append(binding)
        elif category in ("uav", "rtv", "dsv"):
            self._outputs.append(binding)
        elif category == "sampler":
            self._samplers.append(binding)

        if is_texture_descriptor(desc):
            self._textures.append(binding)
        elif is_buffer_descriptor(desc):
            self._buffers.append(binding)

        return binding

    def _collect_stage(self, stage, include_bindings=True, include_shader=False):
        try:
            shader = self.pipe.GetShader(stage)
        except Exception:
            return
        if is_null_handle(shader):
            return
        if include_shader and stage not in self._stage_shaders_loaded:
            self.shaders[stage] = Shader(self, stage)
            self._stage_shaders_loaded.add(stage)

        if not include_bindings or stage in self._stage_bindings_loaded:
            return
        self._stage_bindings_loaded.add(stage)

        for category, getter in (("cbv", self.pipe.GetConstantBlocks),
                                 ("srv", self.pipe.GetReadOnlyResources),
                                 ("uav", self.pipe.GetReadWriteResources),
                                 ("sampler", self.pipe.GetSamplers)):
            try:
                used = getter(stage, False)
            except TypeError:
                used = getter(stage)
            except Exception:
                continue
            for item in used:
                self._add_descriptor_binding(category, item)

    def _collect_outputs(self):
        try:
            for i, desc in enumerate(self.pipe.GetOutputTargets()):
                handle = getattr(desc, "resource", None)
                binding = Binding("rtv", None, i, 0, handle, desc)
                self._outputs.append(binding)
                if is_texture_descriptor(desc):
                    self._textures.append(binding)
        except Exception:
            pass

        try:
            desc = self.pipe.GetDepthTarget()
            handle = getattr(desc, "resource", None)
            if not is_null_handle(handle):
                binding = Binding("dsv", None, 0, 0, handle, desc)
                self._outputs.append(binding)
                if is_texture_descriptor(desc):
                    self._textures.append(binding)
        except Exception:
            pass

    def load_stage_bindings(self, stage):
        self._collect_stage(stage, include_bindings=True, include_shader=False)
        return self

    def load_shaders(self):
        for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Hull, rd.ShaderStage.Domain,
                      rd.ShaderStage.Geometry, rd.ShaderStage.Pixel, rd.ShaderStage.Compute):
            self._collect_stage(stage, include_bindings=False, include_shader=True)
        return self

    def load_outputs(self):
        if not self._outputs_loaded:
            self._collect_outputs()
            self._outputs_loaded = True
        return self

    def load_bindings(self):
        if self._bindings_loaded:
            return
        for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Hull, rd.ShaderStage.Domain,
                      rd.ShaderStage.Geometry, rd.ShaderStage.Pixel, rd.ShaderStage.Compute):
            self._collect_stage(stage, include_bindings=True, include_shader=False)
        self.load_outputs()
        self._bindings_loaded = True
        return self

    def load_all(self):
        self.load_shaders()
        self.load_bindings()
        return self

    def _load_textures(self, stage=None, include_outputs=True):
        if stage is None:
            for st in (rd.ShaderStage.Vertex, rd.ShaderStage.Hull, rd.ShaderStage.Domain,
                       rd.ShaderStage.Geometry, rd.ShaderStage.Pixel, rd.ShaderStage.Compute):
                self.load_stage_bindings(st)
        else:
            self.load_stage_bindings(stage)
        if include_outputs:
            self.load_outputs()

        states = []
        seen = set()
        for binding in self._textures:
            if stage is not None and binding.stage is not None and binding.stage != stage:
                continue
            if not include_outputs and binding.category in ("rtv", "dsv"):
                continue
            state = self.getTexture(binding)
            if state is None:
                continue
            key = str(state.handle)
            if key in seen:
                continue
            seen.add(key)
            states.append(state)
        return states

    def _load_buffers(self, stage=None):
        if stage is None:
            self.load_bindings()
        else:
            self.load_stage_bindings(stage)

        states = []
        seen = set()
        for binding in self._buffers:
            if stage is not None and binding.stage is not None and binding.stage != stage:
                continue
            state = self.getBuffer(binding)
            if state is None:
                continue
            key = str(state.handle)
            if key in seen:
                continue
            seen.add(key)
            states.append(state)
        return states

    def _load_shaders(self, stage=None):
        if stage is not None:
            shader = self.getShader(stage)
            return [shader] if shader is not None else []
        self.load_shaders()
        return [self.shaders[k] for k in sorted(self.shaders.keys(), key=lambda x: int(x))]

    def _load_samplers(self, stage=None):
        if stage is None:
            self.load_bindings()
        else:
            self.load_stage_bindings(stage)
        return [b for b in self._samplers if stage is None or b.stage == stage]

    def _load_inputs(self, stage=None):
        if stage is None:
            self.load_bindings()
        else:
            self.load_stage_bindings(stage)
        return [b for b in self._inputs if stage is None or b.stage == stage]

    def _load_outputs(self, stage=None):
        self.load_outputs()
        if stage is None:
            return list(self._outputs)
        self.load_stage_bindings(stage)
        return [b for b in self._outputs if b.stage is None or b.stage == stage]

    def getTextures(self, stage=None, include_outputs=True):
        return self.getTexture(stage=stage, include_outputs=include_outputs).list()

    def getBuffers(self, stage=None):
        return self.getBuffer(stage=stage).list()

    def pixel_textures(self):
        self.load_stage_bindings(rd.ShaderStage.Pixel)
        return [b for b in self._textures if b.stage == rd.ShaderStage.Pixel and b.category == "srv"]

    def getPixelTextures(self):
        return self.getTextures(stage=rd.ShaderStage.Pixel, include_outputs=False)

    @property
    def pixelShader(self):
        return self.getShader(rd.ShaderStage.Pixel)


class RenderDocTool(object):
    def __init__(self, controller):
        self.controller = controller
        self.resources = list(controller.GetResources())
        self.resource_by_id = {}
        for res in self.resources:
            self.resource_by_id[str(res.resourceId)] = res
        self.textures = {}
        for tex in controller.GetTextures():
            self.textures[str(tex.resourceId)] = tex

    def getResource(self, handle, pass_=None):
        res = self.resource_by_id.get(str(handle))
        tex = self.textures.get(str(handle))
        if tex is not None:
            handle = tex.resourceId
        elif res is not None:
            handle = res.resourceId
        kind = "texture" if tex is not None else "buffer" if res is not None else "resource"
        name = getattr(res, "name", "") if res is not None else str(handle)
        return ResourceState(handle, name, kind, texture=tex, resource=res)

    def getTexture(self, handle, pass_=None):
        state = self.getResource(handle, pass_)
        return state if state.texture is not None else None

    def getBuffer(self, handle, pass_=None):
        state = self.getResource(handle, pass_)
        return state if state.texture is None and not is_null_handle(handle) else None

    def descriptor_state(self, binding):
        state = self.getResource(binding.handle)
        state.descriptor = binding.descriptor
        if is_texture_descriptor(binding.descriptor):
            state.kind = "texture"
        elif is_buffer_descriptor(binding.descriptor):
            state.kind = "buffer"
        return state

    # ----- General-purpose discovery helpers (no replay required) -----

    def filter_textures(self, predicate):
        """Return the texture descriptors that satisfy ``predicate``.

        Operates on the cached ``GetTextures()`` snapshot taken at session
        startup -- no per-event replay is performed, so this is essentially
        free for any predicate cost.
        """
        return [t for t in self.textures.values() if predicate(t)]

    def filter_resources(self, predicate):
        """Return the resources that satisfy ``predicate`` (textures + buffers)."""
        return [r for r in self.resources if predicate(r)]

    def get_usage(self, resource):
        """Return the ``EventUsage`` list for a resource handle / descriptor.

        Wraps ``ReplayController.GetUsage``, which costs a small constant per
        resource and does *not* require ``SetFrameEvent``. Accepts a raw
        ``ResourceId``, a ``TextureDescription``, a ``BufferDescription``, or
        any object exposing ``resourceId``.
        """
        rid = getattr(resource, "resourceId", resource)
        return list(self.controller.GetUsage(rid))

    def find_events_using(self, resources, usage_filter=None):
        """Aggregate event ids that touch any of ``resources``.

        Parameters
        ----------
        resources : iterable
            Resources to inspect; items are anything :meth:`get_usage`
            accepts.
        usage_filter : callable(EventUsage) -> bool, optional
            Keep only entries matching the predicate (for example only
            shader reads). When ``None`` every usage entry is kept.

        Returns
        -------
        dict[int, set[ResourceId]]
            Map from ``eventId`` to the set of resource ids that touched it.

        This is a cheap reverse index for the common "which draws read these
        textures?" question; total cost is one ``GetUsage`` call per
        resource, not per event, so it scales with resource count rather
        than event count.
        """
        by_event = {}
        for res in resources:
            rid = getattr(res, "resourceId", res)
            for u in self.controller.GetUsage(rid):
                if usage_filter is not None and not usage_filter(u):
                    continue
                by_event.setdefault(u.eventId, set()).add(rid)
        return by_event

    def save_texture(self, resource, path, file_type="dds", mip=-1, slice_index=-1,
                     sample_index=None, channel_extract=-1, jpeg_quality=90,
                     type_cast=None):
        """Save a texture through RenderDoc's replay controller.

        ``resource`` may be a ResourceId, TextureDescription, ResourceState, or
        any object exposing ``resourceId``/``handle``. Defaults preserve all mips
        and slices in DDS, which is the fastest/lossless choice for BC textures.
        """
        save = rd.TextureSave()
        rid = getattr(resource, "resourceId", None)
        if rid is None:
            tex = getattr(resource, "texture", None)
            rid = getattr(tex, "resourceId", None) if tex is not None else None
        if rid is None:
            rid = getattr(resource, "handle", resource)
        save.resourceId = rid
        save.destType = file_type_from_name(file_type)
        save.mip = int(mip)
        save.channelExtract = int(channel_extract)
        save.jpegQuality = int(jpeg_quality)
        try:
            save.slice.sliceIndex = int(slice_index)
        except Exception:
            pass
        if sample_index is not None:
            try:
                save.sample.sampleIndex = int(sample_index)
            except Exception:
                pass
        if type_cast is not None:
            save.typeCast = type_cast
        out_dir = os.path.dirname(os.path.abspath(path))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        return self.controller.SaveTexture(save, path)

    def make_pass(self, action, tag=MIN_ACCESS):
        """Build a Pass from the current pipeline state without seeking again."""
        return Pass(self, action, self.controller.GetPipelineState(), tag)

    def get_pass(self, event_id, tag=MIN_ACCESS):
        """Build a Pass for an event id or an Action object.

        Passing an ``Action`` directly avoids the ``find_action`` recursion that
        a bare event id requires, which matters when sweeping a whole capture.
        """
        if hasattr(event_id, "eventId"):
            action = event_id
            event_id = action.eventId
        else:
            action = None
        # force=False lets RenderDoc replay incrementally; force=True forces a
        # full replay from the frame start every call (O(N^2) when sweeping).
        self.controller.SetFrameEvent(event_id, False)
        if action is None:
            action = find_action(self.controller.GetRootActions(), event_id)
            if action is None:
                raise RuntimeError("EID %d not found" % event_id)
        return Pass(self, action, self.controller.GetPipelineState(), tag)

    def fast_pixel_textures(self, event_id):
        """Return ResourceState objects for pixel-shader texture SRVs at an event.

        This avoids constructing a full Pass and scanning all stages/outputs. It is
        intended for broad capture searches where only PS texture bindings matter.
        """
        self.controller.SetFrameEvent(event_id, False)
        pipe = self.controller.GetPipelineState()
        shader = pipe.GetShader(rd.ShaderStage.Pixel)
        if is_null_handle(shader):
            return []

        try:
            used = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False)
        except TypeError:
            used = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel)

        textures = []
        seen = set()
        for used_desc in used:
            desc = getattr(used_desc, "descriptor", None)
            if not is_texture_descriptor(desc):
                continue
            handle = getattr(desc, "resource", None)
            if is_null_handle(handle):
                continue
            key = str(handle)
            if key in seen:
                continue
            seen.add(key)
            state = self.getTexture(handle)
            if state is not None:
                state.descriptor = desc
                textures.append(state)
        return textures

    def iter_passes(self, actions=None, action_filter=is_draw, tag=MIN_ACCESS):
        """Yield ``Pass`` objects across the capture.

        Parameters
        ----------
        actions : iterable of Action, optional
            Pre-flattened list of actions. When ``None`` the controller is
            asked for the full root tree and flattened. Provide an explicit
            list when the caller needs the total up front (e.g. progress
            bars) or wants to scope the sweep.
        action_filter : callable(action) -> bool, optional
            Predicate evaluated *before* ``SetFrameEvent`` -- the dominant
            cost. Defaults to :func:`is_draw`, which excludes markers,
            push-groups, clears, copies, indirect headers, etc. Pass
            ``None`` to disable filtering.
        tag : str
            Eagerness mode forwarded to :class:`Pass`.
        """
        if actions is None:
            actions = flatten_actions(self.controller.GetRootActions())
        for action in actions:
            if action_filter is not None and not action_filter(action):
                continue
            # force=False lets RenderDoc replay incrementally; force=True would
            # re-replay the whole frame each call (turns the loop into O(N^2)).
            self.controller.SetFrameEvent(action.eventId, False)
            yield Pass(self, action, self.controller.GetPipelineState(), tag)


class CaptureSession(object):
    def __init__(self, path):
        self.path = path
        self.cap = None
        self.controller = None

    def __enter__(self):
        self.cap = rd.OpenCaptureFile()
        res = self.cap.OpenFile(self.path, "", None)
        if not is_success(res):
            raise RuntimeError("OpenFile failed: %s" % result_msg(res))
        if not self.cap.LocalReplaySupport():
            raise RuntimeError("Capture does not support local replay")
        res, self.controller = self.cap.OpenCapture(rd.ReplayOptions(), None)
        if self.controller is None or not is_success(res):
            raise RuntimeError("OpenCapture failed: %s" % result_msg(res))
        return RenderDocTool(self.controller)

    def __exit__(self, exc_type, exc, tb):
        if self.controller is not None:
            self.controller.Shutdown()
        if self.cap is not None:
            self.cap.Shutdown()
        self.controller = None
        self.cap = None
        return False


def export_pass(path, event_id, callback, tag=MIN_ACCESS, force=True):
    """Open ``path``, seek to ``event_id``, then invoke ``callback``.

    The callback receives ``(tool, pass_, controller, pipe, action)``. This is
    the high-level pass export entry point for scripts that need the controller
    only after the capture is opened and positioned.
    """
    cap = rd.OpenCaptureFile()
    try:
        res = cap.OpenFile(path, "", None)
        if not is_success(res):
            raise RuntimeError("OpenFile failed: %s" % result_msg(res))
        if not cap.LocalReplaySupport():
            raise RuntimeError("Capture does not support local replay")
        res, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        if controller is None or not is_success(res):
            raise RuntimeError("OpenCapture failed: %s" % result_msg(res))
        try:
            controller.SetFrameEvent(event_id, force)
            action = find_action(controller.GetRootActions(), event_id)
            if action is None:
                raise RuntimeError("EID %d not found" % event_id)
            tool = RenderDocTool(controller)
            pipe = controller.GetPipelineState()
            pass_ = Pass(tool, action, pipe, tag)
            return callback(tool, pass_, controller, pipe, action)
        finally:
            controller.Shutdown()
    finally:
        cap.Shutdown()
