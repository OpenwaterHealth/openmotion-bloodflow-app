# [Experiment Name]

| Field | Value |
|-------|-------|
| **Status** | `prototype` |
| **Owner** | [Your name] |
| **Created** | [YYYY-MM-DD] |
| **Target graduation** | [Quarter/date or "exploratory"] |

## Description

[One paragraph: what this experiment is, what problem it solves, and why it matters to the platform.]

## Approach

[Brief description of the technical approach. What are you trying? What alternatives did you consider?]

## Dependencies

[List any dependencies beyond the base platform package. If this needs specific hardware, say so.]

- Python 3.10+
- `openlifu` (installed package)
- `numpy >= 1.24`

## How to Run

```bash
# Example
cd sandbox/[this-experiment]
pip install -r requirements.txt
python main.py --input sample_data/
```

## Current State

[What works, what doesn't, what's next. Update this as the experiment progresses.]

## Graduation Criteria

[What needs to be true before this can move to production? e.g., "Passes validation against 3 transducer configs", "Achieves <5ms latency on target hardware", "Reviewed by [name]".]

---

*To archive this experiment, update the status table and move the folder to `sandbox/_archived/`. Add an **Archived** date and **Reason** field.*
