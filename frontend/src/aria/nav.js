export const NAV = [
  { id: "generate",    label: "Generate",    icon: "◉" },
  { id: "agent",       label: "Agent",       icon: "⚛" },
  { id: "files",       label: "Files",       icon: "▤" },
  { id: "library",     label: "Library",     icon: "◰" },
  { id: "validate",    label: "Validate",    icon: "⬡" },
  { id: "ecad",        label: "ECAD",        icon: "⊞" },
  { id: "manufacture", label: "Manufacture", icon: "⚙" },
  { id: "runs",        label: "Runs",        icon: "≡" },
];

export const SUB_TABS = {
  generate:    [{ id: "nl", label: "Natural Language" }, { id: "image", label: "From Image" }, { id: "assembly", label: "Assembly" }, { id: "terrain", label: "Terrain" }, { id: "scan", label: "Scan" }, { id: "refine", label: "Refine" }],
  agent:       [{ id: "live", label: "Live Run" }, { id: "trust", label: "Trust Registry" }, { id: "history", label: "History" }],
  files:       [{ id: "browse", label: "Browse" }, { id: "upload", label: "Upload" }],
  library:     [{ id: "parts", label: "Parts" }, { id: "materials", label: "Materials" }, { id: "catalog", label: "Catalog" }],
  validate:    [{ id: "physics", label: "Physics" }, { id: "dfm", label: "DFM" }, { id: "drawings", label: "Drawings" }, { id: "visual", label: "Visual Verify" }, { id: "cem", label: "CEM Advise" }],
  ecad:        [{ id: "schematic", label: "Schematic" }, { id: "layout", label: "PCB Layout" }, { id: "bom", label: "BOM" }, { id: "sim", label: "Simulation" }],
  manufacture: [{ id: "cam", label: "CAM" }, { id: "tools", label: "Tools" }, { id: "post", label: "Post Processors" }],
  runs:        [{ id: "recent", label: "Recent Runs" }, { id: "health", label: "Health" }, { id: "system", label: "System" }],
};
