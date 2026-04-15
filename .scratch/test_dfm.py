import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from aria_os.agents.dfm_agent import run_dfm_analysis

result = run_dfm_analysis('outputs/cad/step/aria_housing.step', goal='aluminium housing')
print(f"Score:   {result['score']}/100")
print(f"Process: {result['process_recommendation']}")
print(f"Passed:  {result['passed']}")
print(f"Issues:  {len(result['issues'])}")
for iss in result['issues'][:5]:
    print(f"  [{iss['severity'].upper()}] {iss['description'][:80]}")
print("Optimizations:")
for opt in result.get('design_suggestions', [])[:3]:
    print(f"  - {str(opt)[:80]}")
print("Done.")
