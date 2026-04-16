"""
build_pdfs.py — Convert POD System markdown docs to professionally styled PDFs.
Usage: python3 docs/build_pdfs.py
Output: docs/Security_Assessment_Report.pdf, docs/Product_Specification.pdf
"""

import re
import sys
import os
from pathlib import Path
import markdown
from weasyprint import HTML, CSS
from weasyprint.text.fonts import FontConfiguration

DOCS_DIR = Path(__file__).parent

# ── CSS ──────────────────────────────────────────────────────
STYLE = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

@page {
    size: A4;
    margin: 22mm 20mm 25mm 20mm;
    @top-left {
        content: string(doc-title);
        font-family: 'Inter', sans-serif;
        font-size: 8pt;
        color: #64748b;
        padding-top: 6mm;
    }
    @top-right {
        content: "POD Automation System";
        font-family: 'Inter', sans-serif;
        font-size: 8pt;
        color: #64748b;
        padding-top: 6mm;
    }
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-family: 'Inter', sans-serif;
        font-size: 8pt;
        color: #94a3b8;
    }
    border-top: 0.5pt solid #e2e8f0;
}

@page :first {
    @top-left { content: ""; }
    @top-right { content: ""; }
    @bottom-center { content: ""; }
    border-top: none;
    margin-top: 0;
}

* {
    box-sizing: border-box;
}

body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 9.5pt;
    line-height: 1.65;
    color: #1e293b;
    background: white;
}

/* ── COVER PAGE ─────────────────────────────── */
.cover {
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 250mm;
    page-break-after: always;
    padding: 20mm 0 10mm 0;
}

.cover-logo-bar {
    width: 60pt;
    height: 4pt;
    background: #1d4ed8;
    margin-bottom: 30pt;
}

.cover-eyebrow {
    font-size: 9pt;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #1d4ed8;
    margin-bottom: 10pt;
}

.cover-title {
    font-size: 28pt;
    font-weight: 700;
    color: #0f172a;
    line-height: 1.15;
    margin-bottom: 8pt;
}

.cover-subtitle {
    font-size: 14pt;
    font-weight: 300;
    color: #475569;
    margin-bottom: 36pt;
}

.cover-divider {
    width: 100%;
    height: 0.5pt;
    background: #e2e8f0;
    margin-bottom: 20pt;
}

.cover-meta table {
    border: none;
    width: auto;
}
.cover-meta td {
    border: none;
    padding: 3pt 24pt 3pt 0;
    font-size: 8.5pt;
    background: none;
}
.cover-meta td:first-child {
    font-weight: 600;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    width: 110pt;
}
.cover-meta td:last-child {
    color: #1e293b;
}

.cover-footer {
    margin-top: auto;
    padding-top: 24pt;
    font-size: 7.5pt;
    color: #94a3b8;
    border-top: 0.5pt solid #f1f5f9;
}

/* ── HEADINGS ──────────────────────────────────── */
h1 {
    font-size: 18pt;
    font-weight: 700;
    color: #0f172a;
    margin: 0 0 4pt 0;
    padding: 0;
    string-set: doc-title content();
}

h2 {
    font-size: 13pt;
    font-weight: 700;
    color: #0f172a;
    margin: 22pt 0 8pt 0;
    padding-bottom: 5pt;
    border-bottom: 1.5pt solid #1d4ed8;
    page-break-after: avoid;
}

h3 {
    font-size: 10.5pt;
    font-weight: 600;
    color: #1e40af;
    margin: 16pt 0 6pt 0;
    page-break-after: avoid;
}

h4 {
    font-size: 9.5pt;
    font-weight: 600;
    color: #334155;
    margin: 12pt 0 4pt 0;
    page-break-after: avoid;
}

/* ── DOCUMENT TITLE BLOCK (top of content) ────── */
.doc-header {
    margin-bottom: 24pt;
}
.doc-header h1 {
    font-size: 20pt;
    margin-bottom: 2pt;
}
.doc-header .subtitle {
    font-size: 11pt;
    color: #64748b;
    font-weight: 300;
}
.doc-header .meta-row {
    margin-top: 12pt;
    font-size: 8pt;
    color: #64748b;
}

/* ── PARAGRAPHS ────────────────────────────────── */
p {
    margin: 0 0 8pt 0;
    orphans: 3;
    widows: 3;
}

/* ── TABLES ────────────────────────────────────── */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 10pt 0 14pt 0;
    font-size: 8.5pt;
    page-break-inside: auto;
}

thead tr {
    background: #1d4ed8;
    color: white;
}

thead th {
    padding: 6pt 9pt;
    text-align: left;
    font-weight: 600;
    font-size: 8pt;
    letter-spacing: 0.03em;
    border: none;
}

tbody tr:nth-child(even) {
    background: #f8fafc;
}

tbody tr:nth-child(odd) {
    background: white;
}

tbody td {
    padding: 5.5pt 9pt;
    border-bottom: 0.5pt solid #e2e8f0;
    vertical-align: top;
    line-height: 1.5;
}

tbody tr:last-child td {
    border-bottom: 1pt solid #cbd5e1;
}

/* ── CODE BLOCKS ───────────────────────────────── */
pre {
    background: #f1f5f9;
    border: 0.5pt solid #e2e8f0;
    border-left: 3pt solid #1d4ed8;
    border-radius: 3pt;
    padding: 10pt 12pt;
    margin: 10pt 0 14pt 0;
    overflow-x: auto;
    page-break-inside: avoid;
}

pre code {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 7.5pt;
    line-height: 1.6;
    color: #1e293b;
    background: none;
    border: none;
    padding: 0;
}

code {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 8pt;
    background: #f1f5f9;
    border: 0.5pt solid #e2e8f0;
    padding: 1pt 4pt;
    border-radius: 2pt;
    color: #1d4ed8;
}

/* ── LISTS ─────────────────────────────────────── */
ul, ol {
    margin: 4pt 0 10pt 0;
    padding-left: 18pt;
}

li {
    margin-bottom: 3pt;
    line-height: 1.55;
}

/* ── BLOCKQUOTE (used for notes) ───────────────── */
blockquote {
    margin: 10pt 0 14pt 0;
    padding: 8pt 12pt;
    background: #eff6ff;
    border-left: 3pt solid #3b82f6;
    border-radius: 0 3pt 3pt 0;
    color: #1e40af;
    font-size: 8.5pt;
}

blockquote p {
    margin: 0;
}

/* ── HORIZONTAL RULE ───────────────────────────── */
hr {
    border: none;
    border-top: 0.5pt solid #e2e8f0;
    margin: 18pt 0;
}

/* ── SECTION BREAK ─────────────────────────────── */
.page-break {
    page-break-before: always;
}

/* ── TABLE OF CONTENTS ─────────────────────────── */
.toc {
    background: #f8fafc;
    border: 0.5pt solid #e2e8f0;
    border-radius: 4pt;
    padding: 14pt 16pt;
    margin: 14pt 0 22pt 0;
    page-break-inside: avoid;
}

.toc h2 {
    font-size: 10pt;
    margin: 0 0 10pt 0;
    border: none;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 600;
}

.toc ol {
    margin: 0;
    padding-left: 16pt;
    counter-reset: none;
}

.toc li {
    font-size: 8.5pt;
    color: #334155;
    margin-bottom: 4pt;
}

/* ── STRONG / EM ────────────────────────────────── */
strong {
    font-weight: 600;
    color: #0f172a;
}

em {
    color: #475569;
}
"""


def md_to_html(md_text: str, title: str, subtitle: str, meta: dict) -> str:
    """Convert markdown to a full HTML document with cover page."""

    # Strip the top-level h1 + subtitle from the markdown body
    # (we render those on the cover page instead)
    lines = md_text.split('\n')
    body_lines = []
    skip_next_blank = False
    for i, line in enumerate(lines):
        if i == 0 and line.startswith('# '):
            continue
        if i == 1 and line.startswith('## ') and 'Product Specification' in line:
            continue
        if i == 1 and line.startswith('## ') and 'Security Assessment' in line:
            continue
        body_lines.append(line)
    md_body = '\n'.join(body_lines).lstrip('\n')

    # Convert markdown to HTML
    md_extensions = ['tables', 'fenced_code', 'codehilite', 'toc', 'nl2br', 'sane_lists']
    html_body = markdown.markdown(md_body, extensions=['tables', 'fenced_code', 'toc', 'sane_lists'])

    # Build cover meta table rows
    meta_rows = ''.join(
        f'<tr><td>{k}</td><td>{v}</td></tr>'
        for k, v in meta.items()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
</head>
<body>

<!-- COVER PAGE -->
<div class="cover">
  <div class="cover-logo-bar"></div>
  <div class="cover-eyebrow">POD Automation System</div>
  <div class="cover-title">{title}</div>
  <div class="cover-subtitle">{subtitle}</div>
  <div class="cover-divider"></div>
  <div class="cover-meta">
    <table>
      {meta_rows}
    </table>
  </div>
  <div class="cover-footer">
    This document is intended for authorised recipients only.<br>
    POD Automation System &mdash; Confidential &amp; Proprietary
  </div>
</div>

<!-- DOCUMENT BODY -->
{html_body}

</body>
</html>"""


def build(md_path: Path, out_path: Path, title: str, subtitle: str, meta: dict):
    print(f"  Building {out_path.name} ...", end=" ", flush=True)
    md_text = md_path.read_text(encoding='utf-8')
    html = md_to_html(md_text, title, subtitle, meta)

    font_config = FontConfiguration()
    css = CSS(string=STYLE, font_config=font_config)
    HTML(string=html, base_url=str(DOCS_DIR)).write_pdf(
        str(out_path),
        stylesheets=[css],
        font_config=font_config,
        optimize_images=True,
    )
    size_kb = out_path.stat().st_size // 1024
    print(f"done ({size_kb} KB)")


if __name__ == "__main__":
    print("\nPOD System — PDF Build\n" + "─" * 40)

    build(
        md_path  = DOCS_DIR / "Security_Assessment_Report.md",
        out_path = DOCS_DIR / "Security_Assessment_Report.pdf",
        title    = "Security Assessment &amp; Remediation Report",
        subtitle = "Internal Security Review",
        meta     = {
            "Document Version": "1.0",
            "Assessment Date":  "March 2026",
            "Classification":   "Customer Disclosure",
            "Standard":         "OWASP Testing Guide v4 / ISO 27001 aligned",
        },
    )

    build(
        md_path  = DOCS_DIR / "Product_Specification.md",
        out_path = DOCS_DIR / "Product_Specification.pdf",
        title    = "Product Specification",
        subtitle = "Technical Reference &amp; Integration Guide",
        meta     = {
            "Document Version": "1.0",
            "Date":             "March 2026",
            "Classification":   "Customer Distribution",
            "Standard":         "ISO/IEC 25010 (Software Product Quality)",
        },
    )

    print("─" * 40)
    print("Output:")
    for f in ["Security_Assessment_Report.pdf", "Product_Specification.pdf"]:
        p = DOCS_DIR / f
        if p.exists():
            print(f"  ✓  docs/{f}  ({p.stat().st_size // 1024} KB)")
        else:
            print(f"  ✗  docs/{f}  NOT FOUND")
    print()
