#!/usr/bin/env python3
"""Check and package the Soluma Sunrise theme for Bulwark Webmail.

    python3 scripts/soluma-sunrise.py            # check, then write dist/soluma-sunrise.zip
    python3 scripts/soluma-sunrise.py --check    # check only

The mark and the app icon come from the Soluma design kit (soluma-io/design-kit), read from a
sibling checkout: ../design-kit/dist/soluma/. Point elsewhere with --brand DIR.

The checks mirror what Bulwark 1.21 does to a theme at install time (lib/extension-package.ts,
lib/theme-compiler.ts, lib/theme-loader.ts), so a clean run here means nothing is stripped there:

- manifest.json has the required fields and valid Theme API v2 fields;
- every token key and value passes the compiler's safety test;
- neither stylesheet contains a pattern Bulwark removes (@import, url(), image-set(), ...);
- theme.css holds only :root and .dark rules (plus @keyframes, @font-face, @media, @supports);
- skin.css stays under 256 KB and theme + skin fit the 200 000 character pre-paint cache;
- the colour pairs the theme sets meet WCAG 2 (4.5 : 1 for text, 3 : 1 for marks and edges).

It also writes:

- preview.svg, the picture on the theme's card in Settings > Appearance (Bulwark reads the
  manifest's "preview" from the ZIP). It is drawn from the manifest's own tokens, light on the
  left and dark on the right, with the mark taken from the design kit's logo/soluma-mark-color.svg;
- icon.png and banner.png, which only the extension directory reads (needs resvg);
- build/compiled.css: the stylesheet Bulwark compiles from the manifest, with theme.css
  appended. For reading and for previews; not part of the ZIP.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # the repo root
SRC = HERE / "themes" / "soluma-sunrise"
OUT_ZIP = HERE / "dist" / "soluma-sunrise.zip"
BUILD = SRC / "build"

# lib/plugin-types.ts: DISALLOWED_CSS_PATTERNS
DISALLOWED = [
    r"@import\b",
    r"url\s*\(\s*(?!['\"]?\s*#)",
    r"image-set\s*\(",
    r"\bimage\s*\(",
    r"\bsrc\s*\(",
    r"expression\s*\(",
    r"javascript\s*:",
    r"-moz-binding",
    r"behavior\s*:",
]
MAX_THEME_ZIP = 2 * 1024 * 1024
MAX_SKIN_BYTES = 256 * 1024
THEME_CACHE_MAX = 200_000
ZIP_FILES = ["manifest.json", "theme.css", "skin.css", "preview.svg"]
def _brand() -> Path:
    """The design kit's built Soluma folder: --brand DIR, else a sibling design-kit checkout."""
    if "--brand" in sys.argv:
        return Path(sys.argv[sys.argv.index("--brand") + 1]).expanduser().resolve()
    return HERE.parent / "design-kit" / "dist" / "soluma"


BRAND = _brand()

errors: list[str] = []
notes: list[str] = []


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# ---------- manifest -> CSS, as lib/theme-compiler.ts does it ----------

SAFE_KEY = re.compile(r"^(--)?[a-z][a-z0-9-]*$")
NAMESPACES = ("color-", "font-", "radius-", "density-")
DENSITY = {
    "compact": {"row-height": "28px", "control-height": "28px", "spacing-1": "2px", "spacing-2": "4px", "spacing-3": "6px"},
    "normal": {"row-height": "36px", "control-height": "32px", "spacing-1": "4px", "spacing-2": "8px", "spacing-3": "12px"},
    "touch": {"row-height": "44px", "control-height": "40px", "spacing-1": "6px", "spacing-2": "12px", "spacing-3": "18px"},
}
PRINT_VARS = {
    "fontFamily": "--print-font-family", "headingFontFamily": "--print-heading-font-family",
    "textColor": "--print-text-color", "headingColor": "--print-heading-color",
    "ruleColor": "--print-rule-color", "pageMargin": "--print-page-margin",
}
TYPO_VARS = {"fontSans": "--font-sans", "fontMono": "--font-mono", "fontDisplay": "--font-display", "baseFontSize": "--font-size-base"}


def token_name(key: str) -> str:
    if key.startswith("--"):
        return key
    if key.startswith(NAMESPACES):
        return f"--{key}"
    return f"--color-{key}"


def safe_value(value: str) -> bool:
    if re.search(r"[{}<>;\\]", value):
        return False
    return not re.search(r"url\s*\(|image-set\s*\(|expression\s*\(|-moz-binding|javascript\s*:", value, re.I)


def emit_tokens(tokens: dict[str, str], where: str) -> list[str]:
    lines = []
    for key, value in tokens.items():
        if not SAFE_KEY.match(key):
            errors.append(f"manifest tokens.{where}.{key}: key must match [a-z0-9-]")
        elif not isinstance(value, str) or not safe_value(value):
            errors.append(f"manifest tokens.{where}.{key}: value would be dropped as unsafe")
        else:
            lines.append(f"  {token_name(key)}: {value.strip()};")
    return lines


def compile_manifest(m: dict) -> str:
    tokens = m.get("tokens", {})
    root = emit_tokens(tokens.get("common", {}), "common") + emit_tokens(tokens.get("light", {}), "light")
    for k, v in m.get("radii", {}).items():
        root.append(f"  --radius-{k}: {v};")
    for k, name in TYPO_VARS.items():
        if k in m.get("typography", {}):
            value = m["typography"][k]
            if not safe_value(value):
                errors.append(f"manifest typography.{k}: value would be dropped as unsafe")
            root.append(f"  {name}: {value};")
    for k, name in PRINT_VARS.items():
        if k in m.get("print", {}):
            root.append(f"  {name}: {m['print'][k]};")
    for k, v in DENSITY.get(m.get("density", ""), {}).items():
        root.append(f"  --density-{k}: {v};")
    dark = emit_tokens(tokens.get("common", {}), "common") + emit_tokens(tokens.get("dark", {}), "dark")
    return ":root {\n" + "\n".join(root) + "\n}\n\n.dark {\n" + "\n".join(dark) + "\n}\n"


def check_manifest(m: dict) -> None:
    for key in ("id", "name", "version", "author", "description", "type"):
        if not isinstance(m.get(key), str) or not m[key].strip():
            errors.append(f'manifest: missing or invalid "{key}"')
    if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", m.get("id", "")):
        errors.append("manifest: id must be lowercase letters, digits and hyphens")
    if m.get("type") != "theme":
        errors.append('manifest: type must be "theme"')
    if not re.match(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", m.get("version", "")):
        errors.append("manifest: version must be semver")
    if "minAppVersion" in m and not re.match(r"^\d+\.\d+\.\d+", m["minAppVersion"]):
        errors.append("manifest: minAppVersion must look like 1.2.3")
    variants = m.get("variants")
    if not variants or any(v not in ("light", "dark") for v in variants):
        errors.append('manifest: variants must be "light" and/or "dark"')
    if m.get("apiVersion") not in (None, 1, 2):
        errors.append("manifest: apiVersion must be 1 or 2")
    if m.get("density") not in (None, "compact", "normal", "touch"):
        errors.append("manifest: density must be compact, normal or touch")


# ---------- stylesheets ----------

def check_patterns(css: str, label: str) -> None:
    text = strip_comments(css)
    for pattern in DISALLOWED:
        for match in re.finditer(pattern, text, re.I):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{label}: Bulwark removes /{pattern}/ (near line {line} of the comment-free text: {text[match.start():match.start() + 40]!r})")
    if "\\" in text:
        notes.append(f"{label}: contains CSS escapes; Bulwark resolves them before checking")
    if text.count("{") != text.count("}") or text.count("(") != text.count(")"):
        errors.append(f"{label}: unbalanced brackets, Bulwark would drop the whole sheet")


def top_level_rules(css: str) -> list[tuple[str, str]]:
    """(prelude, body) for each rule at the top level of a comment-free sheet."""
    rules, depth, start, prelude = [], 0, 0, ""
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                prelude, start = css[start:i].strip(), i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append((prelude, css[start:i]))
                start = i + 1
    return rules


def check_theme_selectors(css: str, label: str = "theme.css") -> None:
    for prelude, body in top_level_rules(strip_comments(css)):
        if re.match(r"^@(-webkit-)?keyframes\b|^@font-face\b", prelude, re.I):
            continue
        if re.match(r"^@(media|supports)\b", prelude, re.I):
            check_theme_selectors(body, f"{label} {prelude}")
            continue
        selectors = [s.strip() for s in prelude.split(",")]
        if not all(s in (":root", ".dark") for s in selectors) or "{" in body:
            errors.append(f'{label}: Bulwark removes the rule for "{prelude[:60]}" (only :root and .dark are kept)')


# ---------- contrast ----------

def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT, MARK = 4.5, 3.0
# (foreground token, background token, needs)
PAIRS = [
    ("foreground", "background", TEXT), ("foreground", "secondary", TEXT), ("foreground", "muted", TEXT),
    ("foreground", "card", TEXT), ("muted-foreground", "background", TEXT), ("muted-foreground", "secondary", TEXT),
    ("muted-foreground", "muted", TEXT), ("muted-foreground", "card", TEXT), ("muted-foreground", "popover", TEXT),
    ("muted-foreground", "accent", TEXT),
    ("primary-foreground", "primary", TEXT), ("secondary-foreground", "secondary", TEXT),
    ("accent-foreground", "accent", TEXT), ("popover-foreground", "popover", TEXT),
    ("destructive-foreground", "destructive", TEXT), ("success-foreground", "success", TEXT),
    ("warning-foreground", "warning", TEXT), ("info-foreground", "info", TEXT),
    ("note-foreground", "note", TEXT), ("selection-foreground", "selection", TEXT),
    ("muted-foreground", "selection", TEXT), ("sidebar-foreground", "sidebar", TEXT),
    ("sidebar-accent-foreground", "sidebar-accent", TEXT),
    ("primary", "background", TEXT), ("destructive", "background", TEXT), ("success", "background", TEXT),
    ("warning", "background", TEXT), ("info", "background", TEXT),
    ("ring", "background", MARK), ("ring", "secondary", MARK), ("input", "background", MARK), ("input", "card", MARK),
    ("unread", "background", MARK), ("unread", "selection", MARK),
]


def check_contrast(m: dict) -> None:
    for variant in m["variants"]:
        t = m["tokens"][variant]
        worst = None
        for fg, bg, needs in PAIRS:
            if not (t.get(fg, "").startswith("#") and t.get(bg, "").startswith("#")):
                continue
            ratio = contrast(t[fg], t[bg])
            if ratio < needs:
                errors.append(f"contrast {variant}: {fg} {t[fg]} on {bg} {t[bg]} is {ratio:.2f} : 1, needs {needs}")
            if worst is None or ratio / needs < worst[0]:
                worst = (ratio / needs, f"{fg} on {bg} {ratio:.2f} : 1 (needs {needs})")
        if worst:
            notes.append(f"contrast {variant}: tightest pair is {worst[1]}")


# ---------- the card Bulwark paints ----------

def card_colours(css: str, selector: str) -> dict[str, str]:
    """What Settings > Appearance reads for the theme's card (components/settings/themes-settings.tsx):
    the first block for the selector, and in it the first value of each token."""
    block = re.search(selector + r"\s*\{([\s\S]*?)\}", css)
    out = {}
    for name in ("background", "sidebar", "card", "primary", "primary-foreground", "foreground", "muted-foreground", "border"):
        found = re.search(rf"--color-{name}\s*:\s*([^;]+);", block.group(1)) if block else None
        out[name] = found.group(1).strip() if found else "(default)"
    return out


# ---------- preview ----------

def mark_paths() -> str:
    """The three steps and their gradients, as drawn in the brand kit's own file."""
    svg = (BRAND / "logo" / "soluma-mark-color.svg").read_text()
    defs = re.search(r"<defs>(.*?)</defs>", svg, re.S).group(1)
    paths = "".join(re.findall(r"<path[^>]*/>", svg))
    return defs, paths


def mock(t: dict[str, str], rail: str, lit: str, read: str, sunken: str) -> str:
    """One variant of the app, 640 wide, drawn from the theme's tokens."""
    bg, fg, muted, border = t["background"], t["foreground"], t["muted-foreground"], t["border"]
    out = [f'<rect width="640" height="400" fill="{bg}"/>']
    # Folder pane
    out.append(f'<rect x="40" width="152" height="400" fill="{t["secondary"]}"/>')
    out.append(f'<rect x="191.5" width="1" height="400" fill="{border}"/>')
    out.append(f'<circle cx="62" cy="24" r="9" fill="{muted}" opacity=".45"/><rect x="78" y="19" width="64" height="5" rx="2.5" fill="{fg}"/><rect x="78" y="28" width="86" height="3" rx="1.5" fill="{muted}" opacity=".7"/>')
    y = 58
    for section in (5, 6):
        out.append(f'<rect x="54" y="{y}" width="42" height="3" rx="1.5" fill="{muted}" opacity=".8"/>')
        y += 14
        for i in range(section):
            selected = section == 6 and i == 0
            if selected:
                out.append(f'<rect x="40" y="{y - 6}" width="152" height="20" fill="{lit}"/><rect x="40" y="{y - 6}" width="3" height="20" fill="url(#rise)"/>')
            hue = [t["chart-1"], t["chart-5"], t["chart-2"], t["chart-3"], t["chart-4"], muted][i % 6]
            out.append(f'<rect x="54" y="{y}" width="8" height="8" rx="2.5" fill="{hue}" opacity=".85"/>')
            out.append(f'<rect x="70" y="{y + 2}" width="{[58, 46, 66, 38, 52, 44][i]}" height="4" rx="2" fill="{fg}" opacity="{1 if selected else .78}"/>')
            if i in (0, 4):
                out.append(f'<rect x="170" y="{y + 2}" width="10" height="4" rx="2" fill="{fg if i == 0 else muted}" opacity=".8"/>')
            y += 20
        y += 12
    # Message list
    out.append(f'<rect x="399.5" width="1" height="400" fill="{border}"/>')
    out.append(f'<rect x="204" y="12" width="184" height="24" rx="8" fill="{t["card"]}" stroke="{t["input"]}"/><rect x="216" y="22" width="60" height="4" rx="2" fill="{muted}" opacity=".6"/>')
    out.append(f'<rect x="192" y="47.5" width="208" height="1" fill="{border}"/>')
    avatars = [t["chart-3"], t["chart-5"], t["chart-2"], t["chart-4"], t["chart-1"], muted]
    for i in range(6):
        y = 48 + i * 58
        unread, current = i < 4 and i != 2, i == 2
        if current:
            out.append(f'<rect x="192" y="{y}" width="208" height="58" fill="{t["selection"]}"/><rect x="192" y="{y}" width="3" height="58" fill="url(#rise)"/>')
        elif not unread:
            out.append(f'<rect x="192" y="{y}" width="208" height="58" fill="{read}"/>')
        if unread:
            out.append(f'<circle cx="200" cy="{y + 29}" r="2.5" fill="{t["unread"]}"/>')
        out.append(f'<circle cx="222" cy="{y + 29}" r="13" fill="{avatars[i]}" opacity=".9"/>')
        weight = 1 if unread else .62
        out.append(f'<rect x="246" y="{y + 15}" width="{[70, 96, 58, 84, 64, 76][i]}" height="5" rx="2.5" fill="{fg}" opacity="{weight}"/>')
        out.append(f'<rect x="366" y="{y + 15}" width="22" height="4" rx="2" fill="{fg if unread else muted}" opacity="{.9 if unread else .7}"/>')
        out.append(f'<rect x="246" y="{y + 27}" width="{[132, 118, 126, 140, 110, 124][i]}" height="4" rx="2" fill="{fg}" opacity="{.9 if unread else .5}"/>')
        out.append(f'<rect x="246" y="{y + 38}" width="{[140, 104, 134, 122, 138, 96][i]}" height="3" rx="1.5" fill="{muted}" opacity=".75"/>')
        out.append(f'<rect x="192" y="{y + 57.5}" width="208" height="1" fill="{border}"/>')
    out.append('<rect x="304" y="350" width="84" height="34" rx="17" fill="url(#step2)"/><rect x="330" y="364" width="42" height="5" rx="2.5" fill="#202125"/><rect x="316" y="362" width="9" height="9" rx="2.5" fill="none" stroke="#202125" stroke-width="1.6"/>')
    # Reading pane
    out.append('<rect x="400" y="200" width="240" height="200" fill="url(#dawn)"/>')
    out.append(f'<rect x="416" y="22" width="150" height="8" rx="4" fill="{fg}"/><rect x="416" y="38" width="104" height="8" rx="4" fill="{fg}"/>')
    out.append(f'<rect x="400" y="63.5" width="240" height="1" fill="{border}"/>')
    out.append(f'<circle cx="430" cy="88" r="12" fill="{avatars[2]}" opacity=".9"/><rect x="450" y="80" width="64" height="5" rx="2.5" fill="{fg}"/><rect x="450" y="91" width="40" height="3" rx="1.5" fill="{muted}" opacity=".75"/>')
    out.append(f'<rect x="400" y="111.5" width="240" height="1" fill="{border}"/>')
    for i, w in enumerate([196, 204, 172, 0, 200, 188, 120]):
        if w:
            out.append(f'<rect x="416" y="{130 + i * 14}" width="{w}" height="4" rx="2" fill="{fg}" opacity=".82"/>')
    out.append(f'<rect x="416" y="240" width="74" height="24" rx="8" fill="{t["primary"]}"/><rect x="430" y="250" width="46" height="4" rx="2" fill="{t["primary-foreground"]}"/>')
    # Rail: dark in both variants
    out.append(f'<rect width="40" height="400" fill="{rail}"/>')
    out.append('<svg x="10" y="13" width="20" height="15.04" viewBox="0 0 500 376">' + MARK[1] + '</svg>')
    for i in range(4):
        y = 48 + i * 32
        if i == 0:
            out.append(f'<rect x="6" y="{y}" width="28" height="28" rx="8" fill="#F6F0E6" opacity=".13"/><rect x="6" y="{y + 7}" width="3" height="14" rx="1.5" fill="url(#rise)"/>')
        out.append(f'<rect x="14" y="{y + 8}" width="12" height="12" rx="3.5" fill="none" stroke="{"#F6F0E6" if i == 0 else "#A8A39A"}" stroke-width="1.6"/>')
    out.append('<circle cx="20" cy="374" r="10" fill="#A8A39A" opacity=".5"/>')
    return "".join(out)


MARK = ("", "")


def write_preview(m: dict, height: int = 400) -> str:
    global MARK
    MARK = mark_paths()
    light, dark = m["tokens"]["light"], m["tokens"]["dark"]
    defs = (
        MARK[0]
        + '<linearGradient id="rise" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#F03D6E"/><stop offset=".36" stop-color="#FF6A45"/><stop offset=".7" stop-color="#FFA640"/><stop offset="1" stop-color="#FFD873"/></linearGradient>'
        + '<linearGradient id="step2" x1="0" x2="1"><stop offset="0" stop-color="#FF6A45"/><stop offset="1" stop-color="#FFA640"/></linearGradient>'
        + '<radialGradient id="dawn" cx=".5" cy="1.15" r=".9"><stop offset="0" stop-color="#FFD873" stop-opacity=".34"/><stop offset=".4" stop-color="#FFA640" stop-opacity=".16"/><stop offset=".7" stop-color="#F03D6E" stop-opacity=".08"/><stop offset="1" stop-color="#F03D6E" stop-opacity="0"/></radialGradient>'
        + '<clipPath id="night"><path d="M372,0H640V400H288Z"/></clipPath>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {(400 - height) // 2} 640 {height}" width="640" height="{height}" role="img" aria-label="Soluma Sunrise, light and dark">'
        f"<defs>{defs}</defs>"
        + mock(light, rail="#202125", lit="#FFFFFF", read="#FBF7F0", sunken="#F6F0E6")
        + '<g clip-path="url(#night)">' + mock(dark, rail="#131416", lit="#2A2B30", read="#1C1D21", sunken="#131416") + "</g>"
        + "</svg>\n"
    )


def write_images(m: dict) -> None:
    (SRC / "preview.svg").write_text(write_preview(m))
    resvg = shutil.which("resvg")
    if not resvg:
        notes.append("resvg not found: icon.png and banner.png were not rebuilt")
        return
    banner_svg = BUILD / "banner.svg"
    banner_svg.write_text(write_preview(m, height=360))
    subprocess.run([resvg, "--width", "1280", str(banner_svg), str(SRC / "banner.png")], check=True)
    icon = BRAND / "icon" / "favicon" / "icon-512.png"
    if icon.exists():
        shutil.copyfile(icon, SRC / "icon.png")


# ---------- run ----------

def main() -> int:
    manifest_text = (SRC / "manifest.json").read_text()
    theme = (SRC / "theme.css").read_text()
    skin = (SRC / "skin.css").read_text()
    try:
        m = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        print(f"manifest.json is not valid JSON: {exc}")
        return 1

    check_manifest(m)
    compiled = compile_manifest(m)
    full_theme = compiled + "\n/* user overrides */\n" + theme
    check_patterns(full_theme, "theme.css")
    check_patterns(skin, "skin.css")
    check_theme_selectors(full_theme)
    if "--color-" not in full_theme:
        errors.append("theme: sets no --color-* variable")
    skin_scope = f'body[data-theme-skin="{m.get("id")}"]'
    if skin_scope not in skin:
        errors.append(f"skin.css: never uses its scope {skin_scope}")
    if re.search(r"\.dark\s+body\[data-theme-skin", skin):
        errors.append('skin.css: ".dark body[data-theme-skin…]" does not match in Bulwark\'s pre-paint copy; read a token instead')
    if len(skin.encode()) > MAX_SKIN_BYTES:
        errors.append(f"skin.css is {len(skin.encode())} bytes; Bulwark drops a skin over {MAX_SKIN_BYTES}")
    cached = len(strip_comments(full_theme)) + len(strip_comments(skin))
    if cached > THEME_CACHE_MAX:
        errors.append(f"theme + skin are {cached} characters; over {THEME_CACHE_MAX} the page paints the default palette first")
    check_contrast(m)
    for variant, selector in (("light", r":root"), ("dark", r"\.dark")):
        notes.append(f"theme card, {variant}: " + ", ".join(f"{k} {v}" for k, v in card_colours(full_theme, selector).items()))

    BUILD.mkdir(exist_ok=True)
    (BUILD / "compiled.css").write_text(full_theme)
    if not errors:
        write_images(m)
    for key, limit in (("preview", 512), ("icon", 256), ("banner", 512)):
        path = SRC / m.get(key, "-")
        if key in m and not path.exists():
            (errors if key == "preview" else notes).append(f'manifest "{key}" names {m[key]}, which is not in the folder')
        elif key in m and path.stat().st_size > limit * 1024:
            errors.append(f"{m[key]} is over {limit} KB")

    for line in notes:
        print(f"  note  {line}")
    for line in errors:
        print(f"  FAIL  {line}")
    print(f"  size  theme {len(full_theme):,} chars compiled, skin {len(skin):,} chars, pre-paint cache {cached:,} / {THEME_CACHE_MAX:,}")
    if errors:
        print(f"{len(errors)} problem(s). No ZIP written.")
        return 1
    if "--check" in sys.argv:
        print("OK")
        return 0

    OUT_ZIP.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ZIP_FILES:
            z.write(SRC / name, name)
    size = OUT_ZIP.stat().st_size
    if size > MAX_THEME_ZIP:
        print(f"  FAIL  {OUT_ZIP.name} is {size:,} bytes; Bulwark's limit is {MAX_THEME_ZIP:,}")
        return 1
    print(f"OK  {OUT_ZIP.relative_to(HERE)} ({size:,} bytes: {', '.join(ZIP_FILES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
