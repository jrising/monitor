"""tools/import_uptimerobot.py: its output must be a valid feed list, with the right checks."""
import sys
from pathlib import Path

import yaml

import server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import import_uptimerobot as imp  # noqa: E402

V2 = [  # the documented v2 getMonitors shape
    {"id": 1, "friendly_name": "Lab website", "url": "https://lab.example.org", "type": 1, "interval": 300, "status": 2},
    {"id": 2, "friendly_name": "Blog", "url": "https://blog.example.org/", "type": 2, "keyword_type": 2,
     "keyword_value": "Database error", "interval": 600, "status": 2},
    {"id": 3, "friendly_name": "Shop", "url": "https://shop.example.org", "type": 2, "keyword_type": 1,
     "keyword_value": "Add to cart", "interval": 3600, "status": 9},
    {"id": 4, "friendly_name": "Mail server", "url": "mail.example.org", "type": 4, "sub_type": 4, "interval": 300, "status": 2},
    {"id": 5, "friendly_name": "Postgres", "url": "db.example.org", "type": 4, "sub_type": 99, "port": 5432, "interval": 60, "status": 2},
    {"id": 6, "friendly_name": "Router", "url": "203.0.113.7", "type": 3, "interval": 300, "status": 2},
    {"id": 7, "friendly_name": "Nightly backup", "url": "", "type": 5, "interval": 86400, "status": 2},
    {"id": 8, "friendly_name": "Old site", "url": "http://old.example.org", "type": 1, "interval": 300, "status": 0},
    {"id": 9, "friendly_name": "Lab website", "url": "https://lab.example.org/api", "type": 1, "interval": 300, "status": 2},
]
V3 = [  # camelCase spellings, string enums
    {"id": 11, "friendlyName": "Wiki", "url": "https://wiki.example.org", "type": "KEYWORD", "keywordType": "ALERT_NOT_EXISTS",
     "keywordValue": "maintenance", "interval": 300, "status": "UP", "httpMethodType": "HEAD"},
    {"id": 12, "friendlyName": "DNS", "url": "example.org", "type": "DNS", "interval": 300, "status": "PAUSED"},
]


def load(text):
    panels, _ = server.parse_feed("panels:\n" + text)
    return {p["id"]: p for p in panels}, {p["id"]: p for p in yaml.safe_load("panels:\n" + text)["panels"]}


def test_v2_conversion_is_valid_and_faithful():
    out = imp.convert(V2)
    panels, raw = load(out)
    assert len(panels) == 9
    assert raw["lab-website"]["check"] == {"type": "http", "url": "https://lab.example.org"}
    assert raw["lab-website"]["schedule"] == "every 5m"
    assert raw["lab-website-2"]["check"]["url"] == "https://lab.example.org/api"          # duplicate names
    assert raw["blog"]["check"]["lacks"] == "Database error" and raw["blog"]["schedule"] == "every 10m"
    assert raw["shop"]["check"]["contains"] == "Add to cart" and raw["shop"]["schedule"] == "every 1h"
    assert raw["mail-server"]["check"] == {"type": "tcp", "host": "mail.example.org", "port": 25}
    assert raw["postgres"]["check"]["port"] == 5432 and raw["postgres"]["schedule"] == "every 5m"  # 60 s → 5 min floor
    assert raw["router"]["check"]["type"] == "tcp"
    assert raw["nightly-backup"]["stale_after"] == "2880m" and "check" not in raw["nightly-backup"]
    assert "ping isn't possible" in out.lower() and "paused in uptimerobot" in out.lower()
    assert "with NOTEs to review" in out.splitlines()[0]


def test_v3_style_fields():
    panels, raw = load(imp.convert(V3, group="sites", priority=True))
    assert raw["wiki"]["check"] == {"type": "http", "url": "https://wiki.example.org", "lacks": "maintenance",
                                    "method": "HEAD"}
    assert raw["wiki"]["priority"] is True and raw["wiki"]["group"] == "sites"
    assert "check" not in raw["dns"]


def test_from_json_file(tmp_path, capsys):
    f = tmp_path / "m.json"
    f.write_text('{"stat": "ok", "monitors": ' + __import__("json").dumps(V2[:2]) + "}")
    assert imp.main(["--from-json", str(f)]) == 0
    panels, _ = load(capsys.readouterr().out)
    assert set(panels) == {"lab-website", "blog"}
