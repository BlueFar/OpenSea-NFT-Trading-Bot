"""Candidates that stop passing: their Info.md folders leave the data folder, and come back if they pass again."""
import os
import tempfile

from src.storage.candidate_files import is_data_problem, purge_removed, remove_info_folder
from src.models.collection import CollectionStats
from tests.mock_data import make_sample_sales_events
from tests.test_mac_runtime_and_chains import _evaluator


def _folders(root):
    out = []
    for dp, dn, fn in os.walk(root):
        if "Info.md" in fn:
            out.append(os.path.relpath(dp, root))
    return sorted(out)


def test_rule_failure_moves_folders_and_passing_again_writes_a_fresh_one():
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as trash:
        ev, store = _evaluator(d)
        ev.trash = trash
        data = ev.config.general.data_root
        assert ev.evaluate_collection("solitary-voyagers", dry_run=False).is_overall_pass
        assert len(_folders(data)) == 1
        good_sales = ev.provider.get_sale_events.return_value
        # Next check: no sales this week, so Trading pace fails
        ev.provider.get_sale_events.return_value = []
        assert ev.evaluate_collection("solitary-voyagers", dry_run=False) is None
        assert _folders(data) == [] and len(os.listdir(trash)) == 1
        assert not [x for x in os.listdir(data) if x.startswith("20")]   # the empty day folder went too
        # Passing again the same day: a fresh Info.md
        ev.provider.get_sale_events.return_value = good_sales
        assert ev.evaluate_collection("solitary-voyagers", dry_run=False).is_overall_pass
        assert len(_folders(data)) == 1


def test_api_failure_or_a_manual_check_keeps_folders():
    with tempfile.TemporaryDirectory() as d:
        ev, store = _evaluator(d)
        ev.trash = None
        data = ev.config.general.data_root
        ev.evaluate_collection("solitary-voyagers", dry_run=False)
        ev.provider.get_sale_events.return_value = None      # OpenSea didn't answer
        ev.evaluate_collection("solitary-voyagers", dry_run=False)
        assert len(_folders(data)) == 1
        ev.provider.get_sale_events.return_value = []
        ev.evaluate_collection("solitary-voyagers", dry_run=True, stop_on_first_failure=False)  # dashboard check
        assert len(_folders(data)) == 1
        ev.config.general.remove_stopped_candidates = False
        ev.evaluate_collection("solitary-voyagers", dry_run=False)
        assert len(_folders(data)) == 1


def test_without_a_trash_folders_go_to_removed_and_are_deleted_after_7_days():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "data")
        p = os.path.join(root, "2026-10-05", "Owls", "Info.md")
        os.makedirs(os.path.dirname(p))
        open(p, "w").write("x")
        open(os.path.join(os.path.dirname(p), "my-notes.txt"), "w").write("mine")
        dest = remove_info_folder(p, root, trash=None)
        assert dest == os.path.join(root, "_removed", "2026-10-05", "Owls")
        assert sorted(os.listdir(dest)) == ["Info.md", "my-notes.txt"]
        assert not os.path.exists(os.path.join(root, "2026-10-05"))
        assert purge_removed(root) == 0
        assert purge_removed(root, now=os.path.getmtime(dest) + 8 * 86400) == 1
        assert not os.path.exists(os.path.join(root, "_removed"))


def test_never_touches_paths_outside_the_data_folder():
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "data")
        os.makedirs(root)
        other = os.path.join(d, "elsewhere", "x", "Info.md")
        os.makedirs(os.path.dirname(other))
        open(other, "w").write("x")
        assert remove_info_folder(other, root) is None and os.path.exists(other)
        deep = os.path.join(root, "a", "b", "c", "Info.md")
        os.makedirs(os.path.dirname(deep))
        open(deep, "w").write("x")
        assert remove_info_folder(deep, root) is None and os.path.exists(deep)
        assert remove_info_folder(os.path.join(root, "2026-10-05", "Gone", "Info.md"), root) is None


def test_data_problems_are_recognised():
    assert is_data_problem("sale events API failed (8-14 days ago)")
    assert is_data_problem("collection details could not be retrieved")
    assert is_data_problem("no floor snapshot from 7d ago yet")
    assert not is_data_problem("Only 0 sales to a collection offer in 14 days (needs 1)")
    assert not is_data_problem("listed 12.0%")


def test_startup_sweep_clears_candidates_that_already_stopped_passing(tmp_path, monkeypatch):
    from src.bot import NFTBot
    import yaml
    raw = yaml.safe_load(open("config/config.yaml"))
    raw["general"]["state_db_path"] = str(tmp_path / "bot.db")
    raw["general"]["data_root"] = str(tmp_path / "data")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    bot = NFTBot(config_path=str(cfg_path))
    bot.evaluator.trash = None
    store, data = bot.state_store, str(tmp_path / "data")
    store.add_discovered_slugs(["owls", "cats", "dogs"])
    for slug in ("owls", "cats", "dogs"):
        p = os.path.join(data, "2026-10-05", slug, "Info.md")
        os.makedirs(os.path.dirname(p))
        open(p, "w").write("x")
        store.record_candidate(slug, "2026-10-05", True, "PASS", details={"info_md": p})
    store.record_candidate("owls", "2026-10-06", False, "listed_items: listed 12%", "listed_items", {"reason": "listed 12%"})
    store.record_candidate("cats", "2026-10-06", False, "x", "trading_frequency", {"reason": "sale events API failed"})
    assert bot.sweep_stopped_candidates() == 1
    assert sorted(os.listdir(os.path.join(data, "2026-10-05"))) == ["cats", "dogs"]
