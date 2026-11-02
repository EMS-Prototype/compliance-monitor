"""Tests for the daily batch runner (the part GitHub Actions executes)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fixtures.server import FixtureServer, STATE  # noqa: E402
from monitor import batch, db  # noqa: E402


@pytest.fixture(scope="module")
def site():
    s = FixtureServer().start()
    yield s
    s.stop()


def write_toml(path, site, extra=""):
    path.write_text(f"""
[[source]]
name = "Circulars"
url = "{site.base}/circulars"
authority = "CBIC"
selector = "#list"

[[source]]
name = "Gated portal"
url = "{site.base}/gate-captcha"
authority = "GSTN"
{extra}""")


def test_sources_toml_validation(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[[source]]\nname = "x"\n')
    with pytest.raises(ValueError, match="missing 'url'"):
        batch.load_sources(p)
    p.write_text('[[source]]\nname = "x"\nurl = "http://a.test"\n[[source]]\nname = "x"\nurl = "http://b.test"\n')
    with pytest.raises(ValueError, match="duplicate"):
        batch.load_sources(p)
    p.write_text("")
    assert batch.load_sources(p) == []


def test_sync_inserts_updates_and_deactivates(tmp_path, site):
    conn = db.connect(str(tmp_path / "m.db"))
    p = tmp_path / "s.toml"
    write_toml(p, site)
    batch.sync_sources(conn, batch.load_sources(p))
    assert [r["name"] for r in conn.execute("SELECT name FROM sources ORDER BY id")] == ["Circulars", "Gated portal"]

    # edit one, drop the other, add a new one
    p.write_text(f'[[source]]\nname = "Circulars"\nurl = "{site.base}/empty"\nselector = "#other"\n'
                 f'[[source]]\nname = "New"\nurl = "{site.base}/empty"\n')
    batch.sync_sources(conn, batch.load_sources(p))
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM sources")}
    assert rows["Circulars"]["url"].endswith("/empty") and rows["Circulars"]["selector"] == "#other"
    assert rows["Circulars"]["id"] == 1                       # same row updated, history kept
    assert rows["Gated portal"]["active"] == 0                # removed from file: deactivated, not deleted
    assert rows["New"]["active"] == 1

    batch.sync_sources(conn, [])                              # empty file deactivates everything
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0] == 0


def test_full_daily_cycle_exports_results_and_detects_change(tmp_path, site):
    STATE["version"] = 1
    toml, dbp, snaps, out = tmp_path / "s.toml", tmp_path / "data" / "m.db", tmp_path / "site/data/snaps", tmp_path / "site/data/results.json"
    write_toml(toml, site)
    args = ["--sources", str(toml), "--db", str(dbp), "--snaps", str(snaps), "--out", str(out), "--allow-private"]

    assert batch.main(args) == 0                              # day 1: baseline
    d1 = json.loads(out.read_text())
    assert d1["summary"] == {"sources": 2, "ok": 1, "blocked": 1, "error": 0, "pending_changes": 0}
    circ = next(s for s in d1["sources"] if s["name"] == "Circulars")
    gate = next(s for s in d1["sources"] if s["name"] == "Gated portal")
    assert circ["status"] == "ok" and any("GSTR-3B due date" in l for l in circ["latest"]["lines"])
    assert circ["latest"]["screenshot"].startswith("snaps/") and circ["latest"]["screenshot"].endswith(".jpg")
    assert (out.parent / circ["latest"]["screenshot"]).exists()
    assert gate["status"] == "blocked" and gate["handoffs"][0]["kind"] == "captcha"
    assert (out.parent / gate["handoffs"][0]["screenshot"]).exists()
    assert d1["changes"] == []

    assert batch.main(args) == 0                              # day 2: nothing changed
    assert json.loads(out.read_text())["changes"] == []

    STATE["version"] = 2
    assert batch.main(args) == 0                              # day 3: the page changed
    d3 = json.loads(out.read_text())
    assert d3["summary"]["pending_changes"] == 1
    ch = d3["changes"][0]
    assert ch["source_name"] == "Circulars" and ch["status"] == "pending"
    assert "extended to 25th" in ch["diff"] and "line(s) added" in ch["summary"]
    assert d3["generated_at"] and d3["run_url"] is None       # run_url only set inside GitHub Actions

    # one clean DB file, no WAL leftovers to commit
    assert dbp.exists() and not Path(str(dbp) + "-wal").exists() and not Path(str(dbp) + "-shm").exists()
    STATE["version"] = 1


def test_prune_keeps_only_what_is_needed(tmp_path, site):
    STATE["version"] = 1
    toml, dbp, snaps, out = tmp_path / "s.toml", tmp_path / "m.db", tmp_path / "snaps", tmp_path / "out/results.json"
    write_toml(toml, site)
    args = ["--sources", str(toml), "--db", str(dbp), "--snaps", str(snaps), "--out", str(out),
            "--allow-private", "--keep", "2"]
    for v in (1, 2, 1, 2, 1):                                  # five runs, each a different page
        STATE["version"] = v
        batch.main(args)
    STATE["version"] = 1
    conn = db.connect(str(dbp))
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE source_id=1").fetchone()[0] >= 3
    txts = sorted(p.name for p in snaps.glob("s1_*.txt"))
    assert len(txts) == 2                                       # only the newest two kept
    newest = conn.execute("SELECT text_path FROM snapshots WHERE source_id=1 ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert newest in txts
    assert len(list(snaps.glob("handoff_s2_*.jpg"))) == 1       # open handoff screenshot survives


def test_private_urls_are_refused_in_ci_mode(tmp_path):
    toml = tmp_path / "s.toml"
    toml.write_text('[[source]]\nname = "metadata"\nurl = "http://169.254.169.254/latest/meta-data/"\n'
                    '[[source]]\nname = "local"\nurl = "http://127.0.0.1:9/"\n')
    out = tmp_path / "o/results.json"
    assert batch.main(["--sources", str(toml), "--db", str(tmp_path / "m.db"), "--snaps", str(tmp_path / "sn"),
                       "--out", str(out)]) == 0                 # no --allow-private
    data = json.loads(out.read_text())
    assert [s["status"] for s in data["sources"]] == ["error", "error"]
    assert all("private or internal" in s["last_error"] for s in data["sources"])
    assert data["sources"][0]["latest"] is None


def test_github_run_url_is_included_when_running_in_actions(tmp_path, site, monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "llakshitt/compliance-monitor")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    toml, out = tmp_path / "s.toml", tmp_path / "o/results.json"
    toml.write_text(f'[[source]]\nname = "c"\nurl = "{site.base}/circulars"\nselector = "#list"\n')
    batch.main(["--sources", str(toml), "--db", str(tmp_path / "m.db"), "--snaps", str(tmp_path / "sn"),
                "--out", str(out), "--allow-private"])
    assert json.loads(out.read_text())["run_url"] == "https://github.com/llakshitt/compliance-monitor/actions/runs/12345"


def test_error_text_is_single_line_and_annotation_escaping(tmp_path, capsys):
    toml, out = tmp_path / "s.toml", tmp_path / "o/results.json"
    toml.write_text('[[source]]\nname = "Ref: a, b"\nurl = "http://127.0.0.1:9/x"\n')   # unreachable
    batch.main(["--sources", str(toml), "--db", str(tmp_path / "m.db"), "--snaps", str(tmp_path / "sn"),
                "--out", str(out), "--allow-private"])
    err = json.loads(out.read_text())["sources"][0]["last_error"]
    assert "\n" not in err and "Call log" not in err
    printed = capsys.readouterr().out
    assert "::warning title=Ref%3A a%2C b::" in printed     # ':' and ',' escaped in the title
    assert printed.count("::warning") == 1                  # one clean line, not split by newlines
