"""Build the editable ER diagram and its PNG using the existing Playwright tools."""

from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "diagrams"
TABLES = (
    ("category", "ResourceCategory", 55, 165, 325, ("PK  id : INTEGER", "name : TEXT UNIQUE")),
    (
        "resource",
        "DepartmentResource",
        505,
        115,
        390,
        (
            "PK  id : INTEGER",
            "name : TEXT",
            "inventory_code : TEXT UNIQUE",
            "FK  category_id : INTEGER",
            "FK  room_id : INTEGER",
            "quantity : INTEGER >= 0",
            "unit_cost : FLOAT >= 0",
        ),
    ),
    ("room", "ResourceRoom", 1020, 165, 325, ("PK  id : INTEGER", "name : TEXT UNIQUE")),
)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    svg = Element("svg", xmlns="http://www.w3.org/2000/svg", width="1400", height="650")
    SubElement(svg, "rect", width="1400", height="650", fill="#f4f7fb")

    def label(x: int, y: int, value: str, size: int = 20, color: str = "#183047") -> None:
        element = SubElement(
            svg,
            "text",
            x=str(x),
            y=str(y),
            fill=color,
            attrib={"font-family": "Arial, sans-serif", "font-size": str(size)},
        )
        element.text = value

    label(55, 56, "Кафедра АРМ — жабдықтар каталогының ER диаграммасы", 30)
    label(55, 89, "SQLModel · SQLite · 1–4 зертханалық жұмыс", 19, "#506478")
    for _, title, x, y, width, fields in TABLES:
        height = 64 + 39 * len(fields)
        SubElement(
            svg,
            "rect",
            x=str(x),
            y=str(y),
            width=str(width),
            height=str(height),
            rx="10",
            fill="white",
            stroke="#b4c3d2",
            attrib={"stroke-width": "2"},
        )
        SubElement(
            svg, "rect", x=str(x), y=str(y), width=str(width), height="54", rx="10", fill="#183047"
        )
        label(x + 18, y + 35, title, 23, "#ffffff")
        for index, field in enumerate(fields):
            label(x + 18, y + 85 + index * 39, field, 19)
    for x1, x2, left, right in ((380, 505, "1", "N"), (895, 1020, "N", "1")):
        SubElement(
            svg,
            "line",
            x1=str(x1),
            y1="255",
            x2=str(x2),
            y2="255",
            stroke="#286fa7",
            attrib={"stroke-width": "3"},
        )
        label(x1 + 12, 240, left, 21)
        label(x2 - 27, 240, right, 21)
    label(55, 515, "Бір санатта бірнеше жабдық болады. Бір бөлмеде бірнеше жабдық орналасады.", 22)
    label(
        55,
        552,
        "Әр жабдық бір санатқа және бір бөлмеге тиесілі. PK — алғашқы кілт, FK — сыртқы кілт.",
        21,
    )
    label(55, 590, "Жабдықтар негізгі АРМ базасында сақталады; кіру құқықтары мен аудит ортақ.", 21)
    svg_path = OUTPUT / "resources-er.svg"
    svg_path.write_bytes(tostring(svg, encoding="utf-8", xml_declaration=True))

    mxfile = Element("mxfile", host="app.diagrams.net")
    diagram = SubElement(mxfile, "diagram", name="Resources ER", id="resources-er")
    model = SubElement(diagram, "mxGraphModel", dx="1400", dy="650", grid="1")
    root = SubElement(model, "root")
    SubElement(root, "mxCell", id="0")
    SubElement(root, "mxCell", id="1", attrib={"parent": "0"})
    for key, title, x, y, width, fields in TABLES:
        cell = SubElement(
            root,
            "mxCell",
            id=key,
            value=title + "\n\n" + "\n".join(fields),
            style="rounded=1;whiteSpace=wrap;html=0;align=left;spacing=16;fontSize=18;",
            vertex="1",
            attrib={"parent": "1"},
        )
        SubElement(
            cell,
            "mxGeometry",
            x=str(x),
            y=str(y),
            width=str(width),
            height=str(75 + 40 * len(fields)),
            attrib={"as": "geometry"},
        )
    for key, target in (("category", "resource"), ("room", "resource")):
        edge = SubElement(
            root,
            "mxCell",
            id=key + "_fk",
            value="1 : N",
            source=key,
            target=target,
            edge="1",
            attrib={"parent": "1"},
            style="edgeStyle=orthogonalEdgeStyle;endArrow=ERmany;startArrow=ERone;",
        )
        SubElement(edge, "mxGeometry", relative="1", attrib={"as": "geometry"})
    (OUTPUT / "resources-er.drawio").write_bytes(
        tostring(mxfile, encoding="utf-8", xml_declaration=True)
    )
    browsers = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    ]
    executable = next((str(path) for path in browsers if path.exists()), None)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        page = browser.new_page(viewport={"width": 1400, "height": 650}, device_scale_factor=1.5)
        page.goto(svg_path.as_uri())
        page.locator("svg").screenshot(path=str(OUTPUT / "resources-er.png"))
        browser.close()
    print("Created resources-er.drawio, resources-er.svg and resources-er.png")


if __name__ == "__main__":
    main()
