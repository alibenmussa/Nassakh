"""Validate a .docx's main parts against the ECMA-376 WordprocessingML schema (Word 2010+ extensions dropped)."""
import sys, zipfile
from lxml import etree
schema = etree.XMLSchema(etree.parse(__file__.rsplit("/", 1)[0] + "/wml.xsd"))
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
z = zipfile.ZipFile(sys.argv[1])
bad = 0
for name in z.namelist():
    if not (name.startswith("word/") and name.endswith(".xml")) or "/theme/" in name or "stylesWithEffects" in name \
            or "webSettings" in name or "fontTable" in name:
        continue
    root = etree.fromstring(z.read(name))
    ignorable = (root.get(f"{{{MC}}}Ignorable") or "").split()
    known = {p: u for p, u in root.nsmap.items() if p}
    drop = {known[p] for p in ignorable if p in known}
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            continue
        if etree.QName(node).namespace in drop and node.getparent() is not None:
            node.getparent().remove(node)
            continue
        for attr in list(node.attrib):
            if attr.startswith("{") and attr[1:].split("}")[0] in drop | {MC}:
                del node.attrib[attr]
    ok = schema.validate(root)
    bad += not ok
    print(name, "valid" if ok else "INVALID")
    for e in list(schema.error_log)[:6]:
        print(f"   line {e.line}: {e.message[:240]}")
sys.exit(1 if bad else 0)
