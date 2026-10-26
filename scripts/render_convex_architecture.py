"""Editable source for public/images/convex-wasm-architecture.{png,svg}.

Regenerate both images after changing this script. The PNG is 1584 × 2376,
twice the theme's 792 CSS-pixel desktop article width.
Requires Python 3, Pillow, and DejaVu Sans (Debian/Ubuntu: fonts-dejavu-core).
From the repository root:

    python3 -m venv .cache/diagram-venv
    .cache/diagram-venv/bin/pip install -r scripts/requirements-diagrams.txt
    .cache/diagram-venv/bin/python scripts/render_convex_architecture.py

For fonts installed elsewhere, pass --font-dir /path/to/fonts containing
DejaVuSans.ttf and DejaVuSans-Bold.ttf. SVG viewers also need DejaVu Sans;
the PNG preserves the rendered text. Commit both generated images with edits.
"""

import argparse
from pathlib import Path
from html import escape
from math import atan2, cos, sin, pi
from PIL import Image, ImageDraw, ImageFont

parser = argparse.ArgumentParser(description='Render the Convex compiler architecture as PNG and SVG.')
parser.add_argument('--font-dir', type=Path, default=Path('/usr/share/fonts/truetype/dejavu'),
                    help='Directory containing DejaVuSans.ttf and DejaVuSans-Bold.ttf.')
args = parser.parse_args()

OUT = Path(__file__).resolve().parents[1] / 'public' / 'images'
W, H, SCALE = 1000, 1500, 3
PNG_WIDTH = 1584
BG = '#f7f9fc'
INK = '#18283e'
MUTED = '#364152'
BORDER = '#d4dee9'
WIRE = '#52657c'
BLUE = '#365fc1'
TEAL = '#087e79'
PURPLE = '#7560a6'

im = Image.new('RGB', (W*SCALE, H*SCALE), BG)
draw = ImageDraw.Draw(im)
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
       '<title>Convex TS function compilation into Wasm and runtime architecture</title>',
       '<desc>TypeScript is analyzed by tsrs and Oxc, bundled by esbuild, deployed to Convex as JavaScript, and additionally compiled by Static Hermes, Emscripten and Wasmtime for native execution.</desc>',
       f'<rect width="{W}" height="{H}" fill="{BG}"/>']
FONT = args.font_dir

def text(x, y, value, size=18, color=INK, bold=False, center=False):
    font = ImageFont.truetype(str(FONT / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')), size*SCALE)
    width = draw.textlength(value, font=font) / SCALE
    if center:
        x -= width/2
    assert x >= 0 and x+width <= W, (value, x, width)
    draw.text((x*SCALE, y*SCALE), value, font=font, fill=color, anchor='lt')
    svg.append(f'<text x="{x}" y="{y+size*0.80}" font-family="DejaVu Sans, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(value)}</text>')
    return width

def rect(x,y,w,h,fill='#ffffff',stroke=BORDER,r=14):
    draw.rounded_rectangle((x*SCALE,y*SCALE,(x+w)*SCALE,(y+h)*SCALE),radius=r*SCALE,fill=fill,outline=stroke,width=1*SCALE)
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')

def line(points,color=WIRE,width=2,dashed=False):
    if dashed:
        for (x1,y1),(x2,y2) in zip(points,points[1:]):
            distance=((x2-x1)**2+(y2-y1)**2)**0.5
            for start in range(0,int(distance),11):
                end=min(start+6,distance)
                draw.line(((x1+(x2-x1)*start/distance)*SCALE,(y1+(y2-y1)*start/distance)*SCALE,
                           (x1+(x2-x1)*end/distance)*SCALE,(y1+(y2-y1)*end/distance)*SCALE),fill=color,width=width*SCALE)
    else:
        draw.line([(x*SCALE,y*SCALE) for x,y in points],fill=color,width=width*SCALE,joint='curve')
    coords=' '.join(f'{x},{y}' for x,y in points)
    dash=' stroke-dasharray="6 5"' if dashed else ''
    svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round"{dash}/>')

def arrow(points,color=WIRE,dashed=False,width=2):
    line(points,color,width,dashed)
    x,y=points[-1]
    px,py=points[-2]
    angle=atan2(y-py,x-px)
    head=[(x,y),(x-10*cos(angle-pi/7),y-10*sin(angle-pi/7)),(x-10*cos(angle+pi/7),y-10*sin(angle+pi/7))]
    draw.polygon([(a*SCALE,b*SCALE) for a,b in head],fill=color)
    svg.append(f'<polygon points="{" ".join(f"{a},{b}" for a,b in head)}" fill="{color}"/>')

def box(x,y,w,h,title,lines=(),artifact=False,accent=BLUE,title_size=23,body_size=16,line_height=23):
    rect(x,y,w,h,fill='#eef3fc' if artifact else '#ffffff',stroke='#cbd7ef' if artifact else BORDER)
    if artifact:
        tx=x+w/2
        content_height=title_size+(7+body_size+(len(lines)-1)*line_height if lines else 0)
        title_y=y+(h-content_height)/2
        body_y=title_y+title_size+7
    else:
        tx=x+20
        title_y=y+14
        body_y=y+43
    title_width=text(tx,title_y,title,title_size,accent if artifact else INK,True,center=artifact)
    assert title_width < w-36, (title,title_width,w)
    for i,value in enumerate(lines):
        line_width=text(tx,body_y+i*line_height,value,body_size,MUTED,center=artifact)
        assert line_width < w-36, (value,line_width,w)

# Separate physical locations and lifecycle phases.
text(50,24,'Convex TS function compilation into Wasm',34,INK,True)
text(50,67,'and runtime architecture',34,INK,True)
rect(25,123,950,912,fill='#f0f4fa',stroke='#d8e1ef',r=18)
rect(25,1050,950,170,fill='#f0f6f6',stroke='#d2e4e1',r=18)
rect(25,1240,950,230,fill='#eaf4f2',stroke='#bcd9d3',r=18)
text(55,144,'Build: convex-wasm-compiler',17,BLUE,True)
text(345,145,'Developer machine / CI',16,MUTED)
text(55,1068,'Deployment',17,TEAL,True)
text(182,1069,'convex-backend · before serving the new Convex app version',16,MUTED)
text(55,1258,'Serving',17,TEAL,True)
text(141,1259,'convex-backend',16,MUTED)

# Ordinary source deployment travels alongside the native artifacts.
arrow([(55,543),(42,543),(42,1145),(55,1145)],BLUE)

box(55,192,890,65,'Application TypeScript + Convex SDK',
    ['Handlers, schema, dependencies and generated API bindings'],title_size=23)

# The checker is a separate consumer of TypeScript, not a part of esbuild.
arrow([(220,257),(220,288)],PURPLE)
arrow([(685,257),(685,288)],BLUE)
box(55,288,330,83,'tsrs + Oxc',
    ['tsrs: types and route metadata','Oxc: reuse / control-flow checks'],accent=PURPLE,title_size=22,body_size=15,line_height=21)
box(425,288,520,83,'esbuild',
    ['TypeScript → JavaScript · bundle & split'],title_size=23)
arrow([(220,371),(220,402)],PURPLE,True)
arrow([(685,371),(685,402)],BLUE)
box(55,402,890,66,'JavaScript modules + build metadata',
    ['Executable code, dependency graph and analysis results'],artifact=True,title_size=23)

# The ordinary source package and its additional native representation.
arrow([(220,468),(220,503)],BLUE)
arrow([(685,468),(685,503)],BLUE)
box(55,503,330,80,'Convex source package',
    ['JavaScript + schema + metadata'],artifact=True,title_size=21,body_size=15)
box(425,503,520,80,'Static Hermes · shermes',
    ['Prepared application JavaScript + bridge → C'],accent=TEAL,title_size=23)
arrow([(685,583),(685,612)],TEAL)
box(425,612,520,60,'Generated C sources',artifact=True,accent=TEAL,title_size=23)

box(55,612,330,102,'Guest runtime libraries',
    ['Hermes + Convex C++ adapter','JavaScript values · Hades GC'],accent=TEAL,title_size=21,body_size=15)
arrow([(685,672),(685,702)],TEAL)
arrow([(385,663),(404,663),(404,752),(425,752)],TEAL)
box(425,702,520,100,'Emscripten · emcc',
    ['Clang / LLVM · wasm-ld · Binaryen','Compile, link and optimize WebAssembly'],accent=TEAL,title_size=23)
arrow([(685,802),(685,835)],TEAL)
box(425,835,520,62,'Core WebAssembly · .wasm',
    ['Portable module graph + authenticated manifest'],artifact=True,accent=TEAL,title_size=22,body_size=15)

# Show the configured deployment workflow: transfer the already compiled AOT.
text(75,777,'FILES SENT TO THE SERVER',13,MUTED,True)
for i,value in enumerate([
    'JavaScript and schema',
    'Portable .wasm modules',
    'Native .cwasm artifacts',
    'Source and compatibility metadata',
]):
    text(75,806+i*23,value,15,MUTED)

arrow([(685,897),(685,930)],TEAL)
box(425,930,520,90,'Wasmtime / Cranelift',
    ['Compile .wasm → native .cwasm','Output matched to the server CPU and engine'],accent=TEAL,title_size=23,body_size=16)
arrow([(685,1020),(685,1095)],TEAL)
box(55,1095,890,100,'Convex deployment + Wasmtime loader',
    ['Verify source match and engine compatibility; map .cwasm code into memory',
     'Initialize reusable guest instances before routing requests to the new version'],accent=TEAL,title_size=23)
arrow([(685,1195),(685,1240)],TEAL)

# Runtime boundary: guest computation invokes backend services.
rect(55,1297,890,151,fill='#f9fcfb',stroke='#bcd9d3',r=12)
text(75,1314,'Tokio runtime',21,TEAL,True)
text(260,1319,'Async request scheduling and host I/O',16,MUTED)
rect(75,1357,345,61,stroke='#bcd9d3',r=10)
text(247.5,1368,'Wasmtime + Hermes',20,INK,True,center=True)
text(247.5,1394,'One invocation fiber · Hades GC',14,MUTED,center=True)
rect(575,1357,350,61,stroke='#bcd9d3',r=10)
text(750,1368,'Convex host services',20,INK,True,center=True)
text(750,1394,'Database · auth · transactions',15,MUTED,center=True)
arrow([(420,1378),(575,1378)],TEAL)
arrow([(575,1402),(420,1402)],TEAL)
text(497.5,1359,'host calls',13,TEAL,center=True)
text(497.5,1407,'results',13,TEAL,center=True)
text(75,1428,'Prepared heaps share copy-on-write memory backing; request state stays private to each instance.',13,MUTED)

line([(50,1486),(85,1486)],WIRE,2)
text(95,1479,'Artifacts / runtime values',13,MUTED)
line([(410,1486),(445,1486)],PURPLE,2,True)
text(455,1479,'Analysis metadata',13,MUTED)

svg.append('</svg>')
OUT.mkdir(parents=True,exist_ok=True)
im.resize((PNG_WIDTH,PNG_WIDTH*H//W),Image.Resampling.LANCZOS).save(OUT/'convex-wasm-architecture.png',optimize=True,dpi=(144,144))
(OUT/'convex-wasm-architecture.svg').write_text('\n'.join(svg)+'\n', encoding='utf-8')
print(OUT/'convex-wasm-architecture.png')
print(OUT/'convex-wasm-architecture.svg')
print('Image dimensions:',Image.open(OUT/'convex-wasm-architecture.png').size)
