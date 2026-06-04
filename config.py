# -*- coding: utf-8 -*-
"""Bootstrap: set up RenderDoc Python module paths from rdc_tool.json.

Import this module before ``import renderdoc`` so that the correct
``pymodules`` directory is injected into ``sys.path`` and the
RenderDoc development directory is added to ``PATH``.
"""

from __future__ import print_function

import json
import os
import sys

CONFIG_FILENAME = "rdc_tool.json"


def _find_config():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.getcwd(), CONFIG_FILENAME),
        os.path.join(here, CONFIG_FILENAME),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _setup():
    cfg_path = _find_config()
    if not cfg_path:
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    rd_cfg = cfg.get("renderdoc") or {}
    dev_dir = rd_cfg.get("development_dir") or rd_cfg.get("renderdoc_dir") or ""
    pymodules = rd_cfg.get("pymodules_dir") or (
        os.path.join(dev_dir, "pymodules") if dev_dir else ""
    )

    if dev_dir and os.path.isdir(dev_dir):
        old_path = os.environ.get("PATH", "")
        sep = os.pathsep
        if dev_dir not in old_path.split(sep):
            os.environ["PATH"] = dev_dir + sep + old_path

    if pymodules and os.path.isdir(pymodules):
        if pymodules not in sys.path:
            sys.path.insert(0, pymodules)


_setup()
