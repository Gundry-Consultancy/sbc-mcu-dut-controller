"""Generate HTML reports showing distinct frames grouped by change type."""

from __future__ import annotations

import html as _html
import os
from pathlib import Path

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Display Report — {test_name}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f5f5f5; }}
  h1 {{ color: #333; }}
  h2 {{ color: #555; border-bottom: 2px solid #ddd; padding-bottom: .3rem; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
  .card {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.12);
           padding: .75rem; max-width: 320px; text-align: center; }}
  .card img {{ max-width: 100%; border-radius: 4px; }}
  .meta {{ font-size: .85rem; color: #666; margin-top: .4rem; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
            font-size: .75rem; font-weight: 600; color: #fff; margin-bottom: .3rem; }}
  .badge-display {{ background: #2563eb; }}
  .badge-led {{ background: #d97706; }}
  .badge-initial {{ background: #6b7280; }}
</style>
</head>
<body>
<h1>Display Test Report</h1>
<p><strong>Test:</strong> {test_name}</p>
<p><strong>Total distinct frames:</strong> {total}</p>
{sections}
</body>
</html>
"""

_SECTION = '<h2>{label} ({count})</h2><div class="grid">{cards}</div>'
_CARD = (
    '<div class="card">'
    '<span class="badge badge-{t}">{t}</span>'
    '<img src="{src}" alt="Frame {n}">'
    '<div class="meta">#{n} &middot; {ts}s</div>'
    "</div>"
)


def generate_report(
    frames: list,
    test_name: str,
    output_dir: str = "artifacts/reports",
) -> str:
    """Generate HTML report from a list of Frame objects; return path to the HTML file."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    groups: dict[str, list] = {}
    for f in frames:
        groups.setdefault(f.change_type, []).append(f)

    sections = ""
    for ctype in ("initial", "display", "led"):
        group = groups.get(ctype, [])
        if not group:
            continue
        cards = "".join(
            _CARD.format(
                t=ctype,
                src=_html.escape(os.path.relpath(f.path, str(out)) if f.path else ""),
                n=f.frame_number,
                ts=f.timestamp_s,
            )
            for f in group
        )
        sections += _SECTION.format(label=ctype.capitalize(), count=len(group), cards=cards)

    html_body = _HTML.format(
        test_name=_html.escape(test_name),
        total=len(frames),
        sections=sections,
    )
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in test_name)
    path = out / f"report_{safe}.html"
    path.write_text(html_body, encoding="utf-8")
    return str(path.resolve())
