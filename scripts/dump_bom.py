import json, pathlib, sys

bundle = sys.argv[1] if len(sys.argv) > 1 else "drone_build"
p = pathlib.Path(__file__).resolve().parent.parent / "outputs" / "system_builds" / bundle
b = json.load((p / "bom.json").open(encoding="utf-8"))
s = b["summary"]
print(f"===== BOM: {bundle} =====")
print(f"  total_parts            : {s['total_parts']}")
print(f"  fabricated_count       : {s['fabricated_count']}")
print(f"  purchased_count        : {s['purchased_count']}")
print(f"  unique_components      : {s['unique_components']}")
print(f"  total_purchased_cost_$ : {s['total_purchased_cost_usd']}")
print(f"  total_mass_g           : {s['total_mass_g']}")
print(f"  ecad_count             : {s['ecad_count']}")
print()
print("===== fabricated[] =====")
for f in b["fabricated"]:
    fname = pathlib.Path(f["step_path"]).name
    print(f"  {f['id']:<10} -> {fname}")
print()
print("===== purchased[] =====")
for r in b["purchased"]:
    qty   = r["quantity"]
    desig = r["designation"]
    cost  = r["total_cost_usd"]
    mass  = r["mass_g"] * qty
    print(f"  {qty:>3}x {desig:<28} ${cost:>7.2f}  {mass:>6.1f}g  ec:{r['export_control']}")
print()
print(f"export_control: {b['export_control']['overall_classification']}")
