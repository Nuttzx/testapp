"""Inject app/data.json into app/template.html -> app/index.html (single self-contained page)."""
import json
d = open("app/data.json").read().replace("</", "<\\/")
t = open("app/template.html").read()
open("app/index.html", "w").write(t.replace("__DATA__", d))
print("app/index.html written")
