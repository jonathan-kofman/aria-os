"""Probe SW interop assemblies for the actual signatures of methods we
need to reflectively invoke from the addin (CreateSectionViewAt5,
AddExplodedView, InsertModelAnnotations*).

Run after a SW addin build so the interop DLLs have been pulled into
obj/Debug/net48 by NuGet. Prints each method's full signature so the
addin's reflection probes can match arg count + types exactly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import clr  # type: ignore  # provided by pythonnet

ADDIN_DIR = Path(__file__).resolve().parents[1] / \
              "cad-plugins" / "solidworks" / "AriaSW" / "obj" / "Debug" / "net48"

# Load the SW interop assemblies we need.
INTEROP_DLLS = [
    "SolidWorks.Interop.sldworks.dll",
    "SolidWorks.Interop.swconst.dll",
]
SEARCH_ROOTS = [
    Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS"),
    Path(r"C:\Program Files\SOLIDWORKS"),
    ADDIN_DIR,
]
for dll in INTEROP_DLLS:
    found = None
    for root in SEARCH_ROOTS:
        if not root.exists(): continue
        # ADDIN_DIR is a leaf; others recurse one level (common is /api/redist
        # or right at install root).
        for cand in [root / dll, root / "api" / "redist" / dll]:
            if cand.is_file():
                found = cand; break
        if found: break
    if not found:
        print(f"  could not find {dll}")
        continue
    print(f"loading {found}")
    clr.AddReference(str(found))

from SolidWorks.Interop.sldworks import IDrawingDoc, IModelDocExtension, \
                                          IConfigurationManager, IAssemblyDoc, \
                                          IModelDoc2, IFeatureManager, \
                                          IConfiguration  # type: ignore


TARGETS = {
    IDrawingDoc: ["CreateSectionViewAt", "CreateSectionViewAt2",
                   "CreateSectionViewAt3", "CreateSectionViewAt4",
                   "CreateSectionViewAt5", "CreateSectionViewAt6",
                   "InsertModelAnnotations", "InsertModelAnnotations2",
                   "InsertModelAnnotations3"],
    IModelDocExtension: ["InsertModelAnnotations", "InsertModelAnnotations2",
                          "InsertModelAnnotations3", "InsertModelAnnotations5",
                          "InsertAutoDimensionScheme"],
    IConfigurationManager: ["AddExplodedView", "AddExplodedView2",
                              "AddConfiguration2", "AddConfiguration3",
                              "ExplodedView", "GetExplodedView"],
    IAssemblyDoc: ["CreateExplodedView", "AddExplodedView",
                    "InsertExplodedView", "ExplodeMultiple"],
    IModelDoc2:   ["FeatureByName", "InsertNote"],
    IConfiguration: ["GetExplodedViewCount", "AddExplodedView",
                       "GetExplodedViewNames"],
    IFeatureManager: ["InsertExplodedView", "InsertSheetMetalBaseFlange",
                        "InsertSheetMetalBaseFlange2", "InsertSurfaceLoft"],
}


def main() -> int:
    from System import Type  # type: ignore
    for iface, names in TARGETS.items():
        clr_type = clr.GetClrType(iface)
        print(f"\n=== {clr_type.FullName} ===")
        methods = [m for m in clr_type.GetMethods()]
        for nm in names:
            ms = [m for m in methods if m.Name == nm]
            if not ms:
                print(f"  {nm}: (not present)")
                continue
            for m in ms:
                params = m.GetParameters()
                psig = ", ".join(
                    f"{p.ParameterType.Name} {p.Name}"
                    for p in params)
                print(f"  {nm}({psig}) -> {m.ReturnType.Name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
